"""Kilo Code CLI provider transport with isolated gateway tool proxies."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
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

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("TUSKER_KILO_CLI_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _model_for_cli(model: str) -> str:
    """Accept an explicit Kilo model ID (provider/model), never a CLI option."""
    value = model.removeprefix("kilo-cli/")
    parts = value.split("/")
    if (
        len(parts) != 2
        or any(not part for part in parts)
        or any(part in {".", ".."} or part.startswith("-") for part in parts)
        or any(not (ch.isalnum() or ch in "._-") for part in parts for ch in part)
    ):
        raise BadRequestError(
            "kilo-cli model must be a provider/model identifier",
            code="unsupported_model",
        )
    return value


class KiloCLIAdapter:
    """Run Kilo in headless mode and proxy client tools through request-scoped MCP."""

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
        executable = self._executable or os.environ.get("TUSKER_KILO_CLI_PATH") or "kilo"
        resolved = shutil.which(executable)
        if not resolved:
            raise ProviderError("Kilo CLI is not installed in the runtime", code="kilo_cli_unavailable")

        tool_manifest = _normalise_tools(tools, tool_choice)
        prompt = _prompt(messages, has_tools=bool(tool_manifest), tool_choice=tool_choice)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/home/tusker"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "USER": os.environ.get("USER", ""),
            "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "")),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "KILO_DISABLE_PROJECT_CONFIG": "true",
        }
        kilo_api_key = os.environ.get("KILO_API_KEY")
        if kilo_api_key:
            env["KILO_API_KEY"] = kilo_api_key
        for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            value = os.environ.get(name)
            if value:
                env[name] = value

        with tempfile.TemporaryDirectory(prefix="tusker-kilo-") as temp_name:
            temp_dir = Path(temp_name)
            call_file: Path | None = None
            config: dict[str, Any] = {
                "permission": {"*": "deny"},
                "plugin": [],
            }
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
                for item in tool_manifest:
                    config["permission"][f"gateway_{item['mcp_name']}"] = "allow"
            env["KILO_CONFIG_CONTENT"] = json.dumps(config, separators=(",", ":"))

            command = [resolved, "run", "--pure", "--format", "json", "--model", cli_model]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )
                communicate_task = asyncio.create_task(proc.communicate(prompt.encode()))
                timeout = max(10.0, float(os.environ.get("TUSKER_KILO_CLI_TIMEOUT_SECS", "600")))
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
                        raise ProviderError("Kilo returned an invalid tool request", code="invalid_upstream_response")
                    completion = ClaudeCodeCLIAdapter._tool_completion(model, tool_call)
                    return ClaudeCodeCLIAdapter._stream_completion(completion, stream) if stream else completion
                stdout, stderr = await communicate_task
            except asyncio.TimeoutError as exc:
                raise ProviderError("Kilo CLI request timed out", code="upstream_timeout") from exc
            except asyncio.CancelledError:
                raise
            except ProviderError:
                raise
            except Exception as exc:
                logger.warning("kilo-cli spawn failed: %s", type(exc).__name__)
                raise ProviderError("Could not start Kilo CLI", code="kilo_cli_failed") from exc

        if proc.returncode != 0:
            logger.warning("kilo-cli exited rc=%s stderr_bytes=%d", proc.returncode, len(stderr))
            raise ProviderError("Kilo CLI request failed", code="kilo_cli_failed")
        text = self._extract_text(stdout)
        if text is None:
            raise ProviderError("Kilo CLI returned no assistant response", code="invalid_upstream_response")
        completion = {
            "id": f"chatcmpl-kilo-{os.urandom(8).hex()}",
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
