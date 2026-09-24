"""Incremental OpenAI SSE framing for JSON-lines CLI adapters."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tusker_gateway.errors import ProviderError
from tusker_gateway.sse import format_openai_chunk, sse_done, sse_frame

logger = logging.getLogger(__name__)


def text_from_event(event: dict[str, Any]) -> str | None:
    """Extract user-visible assistant text from Kilo/OpenCode JSON events."""
    part = event.get("part")
    if event.get("type") == "text" and isinstance(part, dict):
        value = part.get("text")
        return value if isinstance(value, str) and value else None
    return None


def claude_text_from_event(event: dict[str, Any]) -> str | None:
    """Extract Claude Code partial text deltas; ignore lifecycle/thinking events."""
    payload = event.get("event")
    if not isinstance(payload, dict) or payload.get("type") != "content_block_delta":
        return None
    delta = payload.get("delta")
    if isinstance(delta, dict) and delta.get("type") == "text_delta":
        value = delta.get("text")
        return value if isinstance(value, str) and value else None
    return None


def stream_cli_jsonl(
    command: list[str], *, env: dict[str, str], prompt: bytes, model: str,
    timeout: float, text_extractor: Callable[[dict[str, Any]], str | None],
    event_error_extractor: Callable[[dict[str, Any]], Exception | None] | None = None,
    call_file: Path | None = None, cleanup_dir: Path | None = None,
    error_code: str = "cli_failed", timeout_message: str = "CLI request timed out",
):
    """Return an async iterator that emits text as JSON-lines arrive.

    Process output is never treated as model text unless the adapter-specific
    extractor recognizes a text delta. Request-scoped tool calls are still
    surfaced as OpenAI tool-call chunks and are never executed here.
    """

    async def events():
        proc = None
        stderr_task = None
        stream_id = f"chatcmpl-cli-{os.urandom(8).hex()}"
        emitted = False
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, start_new_session=True,
            )
            assert proc.stdin and proc.stdout and proc.stderr
            proc.stdin.write(prompt)
            await proc.stdin.drain()
            proc.stdin.close()
            stderr_task = asyncio.create_task(proc.stderr.read())
            call_value = None
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    raw_line = await asyncio.wait_for(proc.stdout.readline(), min(remaining, 0.05))
                except asyncio.TimeoutError:
                    raw_line = None
                if call_file is not None:
                    try:
                        candidate = json.loads(call_file.read_text(encoding="utf-8"))
                        if isinstance(candidate, dict):
                            call_value = candidate
                    except (OSError, json.JSONDecodeError):
                        pass
                if call_value is not None:
                    from tusker_gateway.provider_adapters.claude_code import ClaudeCodeCLIAdapter

                    if not isinstance(call_value.get("name"), str) or not isinstance(call_value.get("arguments"), dict):
                        raise ProviderError("CLI returned an invalid tool request", code="invalid_upstream_response")
                    completion = ClaudeCodeCLIAdapter._tool_completion(model, call_value)
                    call = completion["choices"][0]["message"]["tool_calls"][0]
                    yield sse_frame({
                        "id": stream_id, "object": "chat.completion.chunk", "model": model,
                        "choices": [{"index": 0, "delta": {"tool_calls": [{
                            "index": 0, "id": call["id"], "type": "function",
                            "function": call["function"],
                        }]}, "finish_reason": None}],
                    })
                    emitted = True
                    break
                if raw_line is None:
                    continue
                if not raw_line:
                    break
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.debug("ignoring malformed CLI progress line")
                    continue
                if isinstance(event, dict):
                    if event_error_extractor is not None:
                        event_error = event_error_extractor(event)
                        if event_error is not None:
                            raise event_error
                    text = text_extractor(event)
                    if text:
                        chunk = format_openai_chunk(content=text, model=model)
                        chunk["id"] = stream_id
                        yield sse_frame(chunk)
                        emitted = True

            if call_value is not None and proc.returncode is None:
                from tusker_gateway.provider_adapters.claude_code import _stop_process
                await _stop_process(proc)
            try:
                await asyncio.wait_for(proc.wait(), max(0.1, deadline - asyncio.get_running_loop().time()))
            except asyncio.TimeoutError as exc:
                from tusker_gateway.provider_adapters.claude_code import _stop_process
                await _stop_process(proc)
                raise ProviderError(timeout_message, code="upstream_timeout") from exc
            stderr = await stderr_task if stderr_task else b""
            if proc.returncode != 0 and call_value is None:
                stderr_preview = stderr.decode("utf-8", errors="replace")[:512]
                logger.warning(
                    "CLI stream exited rc=%s stderr_bytes=%d stderr=%r",
                    proc.returncode, len(stderr), stderr_preview,
                )
                raise ProviderError("CLI request failed", code=error_code)
            if not emitted:
                raise ProviderError("CLI returned no assistant output", code="invalid_upstream_response")
            finish = "tool_calls" if call_value is not None else "stop"
            yield sse_frame({
                "id": stream_id,
                "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            })
            yield sse_done()
        except asyncio.CancelledError:
            if proc is not None:
                from tusker_gateway.provider_adapters.claude_code import _stop_process
                await _stop_process(proc)
            raise
        except asyncio.TimeoutError as exc:
            if proc is not None:
                from tusker_gateway.provider_adapters.claude_code import _stop_process
                await _stop_process(proc)
            raise ProviderError(timeout_message, code="upstream_timeout") from exc
        except OSError as exc:
            raise ProviderError("Could not start CLI process", code=error_code) from exc
        finally:
            if proc is not None and proc.returncode is None:
                from tusker_gateway.provider_adapters.claude_code import _stop_process
                await _stop_process(proc)
            if stderr_task and not stderr_task.done():
                stderr_task.cancel()
            if cleanup_dir is not None:
                shutil.rmtree(cleanup_dir, ignore_errors=True)

    return events()
