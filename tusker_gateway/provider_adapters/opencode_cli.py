"""OpenCode CLI provider transport with isolated gateway tool proxies.

Requests run either against a long-lived ``opencode serve`` (warm) or against a
private per-request server (``--standalone``). The warm server owns provider and
session state, so it removes per-request startup; because its MCP configuration
is fixed when it starts, the shared bridge resolves each request's tools through
a broker pointer (see ``mcp_stdio``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

from tusker_gateway.errors import BadRequestError, ProviderError, ProviderRouteDisabledError
from tusker_gateway.provider_adapters.claude_code import (
    _normalise_tools,
    _prompt,
    _read_tool_call,
    _stop_process,
)
from tusker_gateway.provider_adapters.claude_code import ClaudeCodeCLIAdapter

from tusker_gateway.provider_adapters.kilo_cli import _clear_broker, _point_broker
from tusker_gateway.sse import format_openai_chunk, sse_done, sse_frame

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("TUSKER_OPENCODE_CLI_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _model_for_cli(model: str) -> str:
    value = model if "/" in model else f"opencode/{model}"
    parts = value.split("/")
    if (
        len(parts) != 2
        or parts[0] != "opencode"
        or not parts[1]
        or parts[1] in {".", ".."}
        or parts[1].startswith("-")
        or any(not (ch.isalnum() or ch in "._-") for ch in parts[1])
    ):
        raise BadRequestError("Invalid OpenCode CLI model identifier", code="unsupported_model")
    return value


# --- warm server path -------------------------------------------------------
#
# ``opencode run --standalone`` starts a private HTTP server per request; a
# long-lived server keeps provider, auth, and session state warm, which is worth
# seconds on a trivial turn. Its MCP configuration is fixed when the server
# starts, so tool requests resolve their manifest through the shared bridge
# pointer and run one at a time.

_WARM_SERVICE_URL: str | None = None
_WARM_SERVICE_LOCK = asyncio.Lock()
_WARM_REQUEST_LOCK = asyncio.Lock()
_WARM_IDLE_TASK: asyncio.Task[None] | None = None
_WARM_LAST_USE = 0.0
_SERVICE_URL_RE = re.compile(r"https?://[^\s\"']+")


def _warm_enabled() -> bool:
    return os.environ.get("TUSKER_OPENCODE_WARM_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _warm_broker_dir() -> Path:
    override = os.environ.get("TUSKER_OPENCODE_WARM_BROKER_DIR", "").strip()
    return Path(override) if override else Path(tempfile.gettempdir()) / "tusker-opencode-broker"


def _warm_idle_secs() -> float:
    try:
        return max(0.0, float(os.environ.get("TUSKER_OPENCODE_WARM_IDLE_SECS", "300")))
    except ValueError:
        return 300.0


def _service_url(output: bytes) -> str | None:
    match = _SERVICE_URL_RE.search(output.decode("utf-8", errors="replace"))
    return match.group(0).rstrip("/") if match else None


def _warm_bridge_config(broker_dir: Path) -> dict[str, Any]:
    """MCP config for the shared warm server: one bridge, request-scoped tools."""
    return {"mcp": {"gateway": {
        "type": "local",
        "command": [sys.executable, "-m", "tusker_gateway.provider_adapters.mcp_stdio"],
        "environment": {"TUSKER_MCP_BROKER_DIR": str(broker_dir)},
        "timeout": 120000,
    }}}


async def _run_cli_tool(
    binary: str, args: list[str], env: dict[str, str], *, timeout: float = 30.0,
) -> tuple[int, bytes, bytes]:
    """Run a short-lived control command (service lifecycle, session control)."""
    proc = await asyncio.create_subprocess_exec(
        binary, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        await _stop_process(proc)
        return 124, b"", b""
    return proc.returncode if proc.returncode is not None else 0, stdout, stderr


async def _ensure_warm_service(binary: str, env: dict[str, str]) -> str | None:
    """Return the URL of our warm server, starting or replacing it as needed.

    A service this process did not configure cannot serve the shared bridge
    pointer, so any other service (for example one auto-started by a client) is
    replaced.
    """
    global _WARM_SERVICE_URL

    async with _WARM_SERVICE_LOCK:
        if _WARM_SERVICE_URL is not None:
            code, stdout, _ = await _run_cli_tool(binary, ["service", "status"], env)
            if code == 0 and _service_url(stdout) == _WARM_SERVICE_URL:
                return _WARM_SERVICE_URL
            _WARM_SERVICE_URL = None
        broker_dir = _warm_broker_dir()
        try:
            broker_dir.mkdir(parents=True, exist_ok=True)
            _clear_broker(broker_dir / "active.json")
        except OSError as exc:
            logger.warning("opencode warm broker unavailable (%s)", type(exc).__name__)
            return None
        service_env = dict(env)
        service_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            _warm_bridge_config(broker_dir), separators=(",", ":"),
        )
        await _run_cli_tool(binary, ["service", "stop"], env, timeout=60)
        code, stdout, stderr = await _run_cli_tool(
            binary, ["service", "start"], service_env, timeout=180,
        )
        url = _service_url(stdout) if code == 0 else None
        if not url:
            logger.warning(
                "opencode warm service unavailable rc=%s stderr=%r",
                code, stderr.decode("utf-8", errors="replace")[:256],
            )
            return None
        _WARM_SERVICE_URL = url
        logger.info("opencode warm service ready url=%s", url)
        _schedule_warm_idle_stop(binary, env)
        return url


def _schedule_warm_idle_stop(binary: str, env: dict[str, str]) -> None:
    global _WARM_IDLE_TASK

    idle = _warm_idle_secs()
    if idle <= 0 or (_WARM_IDLE_TASK is not None and not _WARM_IDLE_TASK.done()):
        return
    _WARM_IDLE_TASK = asyncio.ensure_future(_warm_idle_watchdog(binary, env, idle))


async def _warm_idle_watchdog(binary: str, env: dict[str, str], idle: float) -> None:
    """Release the server, and its memory, once traffic stops."""
    global _WARM_SERVICE_URL

    try:
        while True:
            await asyncio.sleep(min(30.0, idle))
            if asyncio.get_running_loop().time() - _WARM_LAST_USE < idle:
                continue
            async with _WARM_SERVICE_LOCK:
                if _WARM_SERVICE_URL is None:
                    return
                await _run_cli_tool(binary, ["service", "stop"], env, timeout=60)
                _WARM_SERVICE_URL = None
            logger.info("opencode warm service stopped after %.0fs idle", idle)
            return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - a watchdog must never break serving
        logger.debug("opencode warm idle watchdog ended (%s)", type(exc).__name__)


async def _control_session(binary: str, env: dict[str, str], session_id: str, action: str) -> None:
    """Interrupt or delete a session on the warm server; never fail a request."""
    args = (
        ["api", "POST", f"/api/session/{session_id}/interrupt"]
        if action == "interrupt"
        else ["api", "DELETE", f"/api/session/{session_id}"]
    )
    try:
        code, _, stderr = await _run_cli_tool(binary, args, env, timeout=20)
        if code != 0:
            logger.debug(
                "opencode session %s rc=%s stderr=%r",
                action, code, stderr.decode("utf-8", errors="replace")[:200],
            )
    except Exception as exc:
        logger.debug("opencode session %s failed (%s)", action, type(exc).__name__)


def _call_payload(call_file: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(call_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


async def _warm_events(
    *, binary: str, cli_model: str, prompt: str, env: dict[str, str],
    tool_manifest: list[dict[str, Any]],
) -> AsyncIterator[tuple[str, Any]]:
    """Yield ``("text", delta)`` as it arrives, then one terminal event.

    The terminal event is ``("tool_call", payload)`` when the request's tool was
    called, otherwise ``("end", text)``. Requests are serialized because the
    shared bridge pointer names exactly one in-flight request: a concurrent
    request could otherwise observe another request's tools.
    """
    from tusker_gateway.provider_adapters.cli_streaming import text_from_event

    global _WARM_LAST_USE

    timeout = max(10.0, float(os.environ.get("TUSKER_OPENCODE_CLI_TIMEOUT_SECS", "600")))
    async with _WARM_REQUEST_LOCK:
        _WARM_LAST_USE = asyncio.get_running_loop().time()
        request_dir = Path(tempfile.mkdtemp(prefix="tusker-opencode-warm-"))
        pointer = _warm_broker_dir() / "active.json"
        manifest_file = request_dir / "tools.json"
        call_file = request_dir / "tool-call.json"
        proc: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        session_id: str | None = None
        try:
            if tool_manifest:
                manifest_file.write_text(json.dumps(tool_manifest), encoding="utf-8")
                _point_broker(pointer, manifest_file, call_file)
            else:
                _clear_broker(pointer)
            proc = await asyncio.create_subprocess_exec(
                binary, "run", "--format", "json", "--model", cli_model,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=request_dir,
                start_new_session=True,
            )
            assert proc.stdin and proc.stdout and proc.stderr
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            stderr_task = asyncio.create_task(proc.stderr.read())
            deadline = asyncio.get_running_loop().time() + timeout
            chunks: list[str] = []
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    raw_line = await asyncio.wait_for(proc.stdout.readline(), min(remaining, 0.05))
                except asyncio.TimeoutError:
                    raw_line = None
                if tool_manifest:
                    payload = _call_payload(call_file)
                    if payload is not None:
                        if (
                            not isinstance(payload.get("name"), str)
                            or not isinstance(payload.get("arguments"), dict)
                        ):
                            raise ProviderError(
                                "OpenCode returned an invalid tool request",
                                code="invalid_upstream_response",
                            )
                        if session_id:
                            # The server keeps generating after the client exits.
                            await _control_session(binary, env, session_id, "interrupt")
                        await _stop_process(proc)
                        yield ("tool_call", payload)
                        return
                if raw_line is None:
                    continue
                if not raw_line:
                    break
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                found = event.get("sessionID")
                if isinstance(found, str) and found:
                    session_id = found
                text = text_from_event(event)
                if text:
                    chunks.append(text)
                    yield ("text", text)
            try:
                await asyncio.wait_for(
                    proc.wait(), max(0.1, deadline - asyncio.get_running_loop().time()),
                )
            except asyncio.TimeoutError as exc:
                await _stop_process(proc)
                raise ProviderError("OpenCode CLI request timed out", code="upstream_timeout") from exc
            stderr = await stderr_task if stderr_task is not None else b""
            if proc.returncode != 0:
                logger.warning(
                    "opencode-cli warm exited rc=%s stderr_bytes=%d stderr=%r",
                    proc.returncode, len(stderr),
                    stderr.decode("utf-8", errors="replace")[:512],
                )
                raise ProviderError("OpenCode CLI request failed", code="opencode_cli_failed")
            text = "".join(chunks)
            if not text:
                raise ProviderError(
                    "OpenCode CLI returned no assistant response",
                    code="invalid_upstream_response",
                )
            yield ("end", text)
        except asyncio.TimeoutError as exc:
            if proc is not None:
                await _stop_process(proc)
            raise ProviderError("OpenCode CLI request timed out", code="upstream_timeout") from exc
        except (asyncio.CancelledError, GeneratorExit):
            if proc is not None:
                await _stop_process(proc)
            raise
        finally:
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            if tool_manifest:
                _clear_broker(pointer)
            if session_id:
                await _control_session(binary, env, session_id, "delete")
            shutil.rmtree(request_dir, ignore_errors=True)


def _child_env() -> dict[str, str]:
    """Allowlisted child environment shared by every OpenCode CLI invocation."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/home/tusker"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "USER": os.environ.get("USER", ""),
        "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "")),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
    }
    # Respect OpenCode's own auth store by default. OPENCODE_ZEN_API_KEY is the
    # gateway HTTP provider credential and is not interchangeable with the CLI's
    # OPENCODE_API_KEY; silently aliasing it can override a valid CLI login with
    # a key that Zen rejects for CLI use.
    opencode_api_key = (
        os.environ.get("OPENCODE_API_KEY")
        or os.environ.get("TUSKER_OPENCODE_CLI_API_KEY")
    )
    if opencode_api_key:
        env["OPENCODE_API_KEY"] = opencode_api_key
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "OPENCODE_TEST_HOME"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    package_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (package_root, os.environ.get("PYTHONPATH", "")) if part
    )
    return env


class OpenCodeCLIAdapter:
    """Run OpenCode in an isolated headless process and proxy client tools."""

    def __init__(self, *, executable: str | None = None) -> None:
        self._executable = executable

    async def chat(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> dict[str, Any] | Any:
        if not _enabled():
            raise ProviderRouteDisabledError(provider)
        cli_model = _model_for_cli(model)
        # Prefer an explicit operator path. PATH lookup can otherwise select
        # an older Homebrew install ahead of a separately installed v2 CLI.
        executable = self._executable or os.environ.get("TUSKER_OPENCODE_CLI_PATH") or "opencode"
        resolved = shutil.which(executable)
        if not resolved:
            raise ProviderError("OpenCode CLI is not installed in the runtime", code="opencode_cli_unavailable")

        tool_manifest = _normalise_tools(tools, tool_choice)
        prompt = _prompt(
            messages, has_tools=bool(tool_manifest), tool_choice=tool_choice,
            provider_label="opencode-cli",
        )
        env = _child_env()

        if _warm_enabled():
            warm = await self._warm_request(
                binary=resolved,
                cli_model=cli_model,
                model=model,
                prompt=prompt,
                tool_manifest=tool_manifest,
                stream=stream,
            )
            if warm is not None:
                return warm

        if stream:
            from tusker_gateway.provider_adapters.cli_streaming import stream_cli_jsonl, text_from_event

            temp_dir = Path(tempfile.mkdtemp(prefix="tusker-opencode-stream-"))
            call_file: Path | None = None
            config: dict[str, Any] = {}
            if tool_manifest:
                manifest_file = temp_dir / "tools.json"
                call_file = temp_dir / "tool-call.json"
                manifest_file.write_text(json.dumps(tool_manifest), encoding="utf-8")
                config["mcp"] = {
                    "gateway": {
                        "type": "local",
                        "command": [sys.executable, "-m", "tusker_gateway.provider_adapters.mcp_stdio"],
                        "environment": {
                            "TUSKER_MCP_MANIFEST": str(manifest_file),
                            "TUSKER_MCP_CALL_FILE": str(call_file),
                        },
                        "timeout": 120000,
                    },
                }
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, separators=(",", ":"))
            command = [resolved, "run", "--standalone", "--format", "json", "--model", cli_model]
            timeout = max(10.0, float(os.environ.get("TUSKER_OPENCODE_CLI_TIMEOUT_SECS", "600")))
            return stream_cli_jsonl(
                command, env=env, prompt=prompt.encode(), model=model, timeout=timeout,
                text_extractor=text_from_event, call_file=call_file, cleanup_dir=temp_dir,
                error_code="opencode_cli_failed", timeout_message="OpenCode CLI request timed out",
            )

        with tempfile.TemporaryDirectory(prefix="tusker-opencode-") as temp_name:
            temp_dir = Path(temp_name)
            call_file: Path | None = None
            config: dict[str, Any] = {}
            if tool_manifest:
                manifest_file = temp_dir / "tools.json"
                call_file = temp_dir / "tool-call.json"
                manifest_file.write_text(json.dumps(tool_manifest), encoding="utf-8")
                config["mcp"] = {
                    "gateway": {
                        "type": "local",
                        "command": [
                            sys.executable,
                            "-m", "tusker_gateway.provider_adapters.mcp_stdio",
                        ],
                        "environment": {
                            "TUSKER_MCP_MANIFEST": str(manifest_file),
                            "TUSKER_MCP_CALL_FILE": str(call_file),
                        },
                        "timeout": 120000,
                    },
                }
                # Do not set OpenCode's `permission` config here. OpenCode Zen
                # free-tier auth rejects CLI requests with custom permission
                # rules (403), while MCP-only config works and its run command
                # auto-rejects permission prompts in non-interactive mode.
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, separators=(",", ":"))
            # The model must not inherit the gateway's working tree as its
            # project. Keep its workspace request-scoped and empty.

            command = [
                resolved, "run", "--standalone", "--format", "json",
                "--model", cli_model,
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=temp_dir,
                    start_new_session=True,
                )
                communicate_task = asyncio.create_task(proc.communicate(prompt.encode()))
                timeout = max(10.0, float(os.environ.get("TUSKER_OPENCODE_CLI_TIMEOUT_SECS", "600")))
                tool_call = None
                if call_file is not None:
                    call_task = asyncio.create_task(_read_tool_call(call_file, communicate_task))
                    try:
                        done, _ = await asyncio.wait(
                            {communicate_task, call_task}, timeout=timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if call_task in done:
                            tool_call = call_task.result()
                        if tool_call is not None:
                            await _stop_process(proc)
                            try:
                                await asyncio.wait_for(communicate_task, 3)
                            except asyncio.TimeoutError:
                                communicate_task.cancel()
                            call_task.cancel()
                        elif communicate_task not in done:
                            await _stop_process(proc)
                            communicate_task.cancel()
                            call_task.cancel()
                            raise asyncio.TimeoutError
                        else:
                            call_task.cancel()
                    except asyncio.CancelledError:
                        await _stop_process(proc)
                        communicate_task.cancel()
                        call_task.cancel()
                        raise
                else:
                    try:
                        await asyncio.wait_for(asyncio.shield(communicate_task), timeout)
                    except asyncio.TimeoutError:
                        await _stop_process(proc)
                        communicate_task.cancel()
                        raise
                if tool_call is not None:
                    if not isinstance(tool_call.get("name"), str) or not isinstance(tool_call.get("arguments"), dict):
                        raise ProviderError("OpenCode returned an invalid tool request", code="invalid_upstream_response")
                    completion = ClaudeCodeCLIAdapter._tool_completion(model, tool_call)
                    return ClaudeCodeCLIAdapter._stream_completion(completion, stream) if stream else completion
                stdout, stderr = await communicate_task
            except asyncio.TimeoutError as exc:
                raise ProviderError("OpenCode CLI request timed out", code="upstream_timeout") from exc
            except asyncio.CancelledError:
                raise
            except ProviderError:
                raise
            except Exception as exc:
                logger.warning("opencode-cli spawn failed: %s", type(exc).__name__)
                raise ProviderError("Could not start OpenCode CLI", code="opencode_cli_failed") from exc

        if proc.returncode != 0:
            stderr_preview = stderr.decode("utf-8", errors="replace")[:512]
            logger.warning(
                "opencode-cli exited rc=%s stderr_bytes=%d stderr=%r",
                proc.returncode, len(stderr), stderr_preview,
            )
            raise ProviderError("OpenCode CLI request failed", code="opencode_cli_failed")
        text = self._extract_text(stdout)
        if text is None:
            raise ProviderError("OpenCode CLI returned no assistant response", code="invalid_upstream_response")
        completion = {
            "id": f"chatcmpl-opencode-{os.urandom(8).hex()}",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return ClaudeCodeCLIAdapter._stream_completion(completion, stream) if stream else completion

    async def _warm_request(
        self,
        *,
        binary: str,
        cli_model: str,
        model: str,
        prompt: str,
        tool_manifest: list[dict[str, Any]],
        stream: bool,
    ) -> dict[str, Any] | AsyncIterator[bytes] | None:
        """Serve this request from the warm server, or return None to fall back.

        A missing or unstartable server falls back before any bytes are sent.
        Non-streaming failures fall back as well; a streaming failure after the
        first frame propagates, because the client already holds partial output.
        """
        env = _child_env()
        try:
            if await _ensure_warm_service(binary, env) is None:
                return None
        except Exception as exc:
            logger.warning("opencode warm service unavailable (%s)", type(exc).__name__)
            return None
        events = _warm_events(
            binary=binary,
            cli_model=cli_model,
            prompt=prompt,
            env=env,
            tool_manifest=tool_manifest,
        )
        if stream:
            return self._warm_stream(events, model=model)
        try:
            return await self._warm_completion(events, model=model)
        except Exception as exc:
            logger.warning(
                "opencode warm request failed (%s); using isolated CLI", type(exc).__name__,
            )
            return None

    async def _warm_completion(
        self, events: AsyncGenerator[tuple[str, Any], None], *, model: str,
    ) -> dict[str, Any]:
        text: str | None = None
        try:
            async for kind, value in events:
                if kind == "tool_call":
                    # A tool call ends the turn: release the worker deterministically
                    # instead of leaving it suspended until garbage collection.
                    return ClaudeCodeCLIAdapter._tool_completion(model, value)
                if kind == "end":
                    text = value
        finally:
            await events.aclose()
        if not text:
            raise ProviderError(
                "OpenCode CLI returned no assistant response", code="invalid_upstream_response",
            )
        return {
            "id": f"chatcmpl-opencode-{os.urandom(8).hex()}",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def _warm_stream(
        self, events: AsyncGenerator[tuple[str, Any], None], *, model: str,
    ) -> AsyncIterator[bytes]:
        """Close the worker generator when the caller stops reading frames."""
        try:
            async for frame in self._warm_frames(events, model=model):
                yield frame
        except GeneratorExit:
            await events.aclose()
            raise

    async def _warm_frames(
        self, events: AsyncIterator[tuple[str, Any]], *, model: str,
    ) -> AsyncIterator[bytes]:
        stream_id = f"chatcmpl-opencode-{os.urandom(8).hex()}"
        finish = "stop"
        async for kind, value in events:
            if kind == "text":
                chunk = format_openai_chunk(content=value, model=model)
                chunk["id"] = stream_id
                yield sse_frame(chunk)
            elif kind == "tool_call":
                completion = ClaudeCodeCLIAdapter._tool_completion(model, value)
                call = completion["choices"][0]["message"]["tool_calls"][0]
                finish = "tool_calls"
                tool_frame = format_openai_chunk(model=model)
                tool_frame["id"] = stream_id
                tool_frame["choices"] = [{
                    "index": 0,
                    "delta": {"tool_calls": [{
                        "index": 0,
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["function"]["name"],
                            "arguments": call["function"]["arguments"],
                        },
                    }]},
                    "finish_reason": None,
                }]
                yield sse_frame(tool_frame)
        finish_frame = format_openai_chunk(finish_reason=finish, model=model)
        finish_frame["id"] = stream_id
        yield sse_frame(finish_frame)
        yield sse_done()

    @staticmethod
    def _extract_text(stdout: bytes) -> str | None:
        text: list[str] = []
        try:
            for raw_line in stdout.decode("utf-8").splitlines():
                if not raw_line.strip():
                    continue
                event = json.loads(raw_line)
                if not isinstance(event, dict) or event.get("type") != "text":
                    continue
                part = event.get("part")
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text.append(part["text"])
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return "".join(text) if text else None
