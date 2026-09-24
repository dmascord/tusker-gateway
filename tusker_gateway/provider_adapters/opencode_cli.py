"""OpenCode CLI provider transport with isolated gateway tool proxies."""

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
        prompt = _prompt(messages, has_tools=bool(tool_manifest), tool_choice=tool_choice)
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
        # Respect OpenCode's own auth store by default. OPENCODE_ZEN_API_KEY is
        # the gateway HTTP provider credential and is not interchangeable with
        # the CLI's OPENCODE_API_KEY; silently aliasing it can override a valid
        # CLI login with a key that Zen rejects for CLI use.
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
            package_root = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (package_root, os.environ.get("PYTHONPATH", "")) if part
            )
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
            package_root = str(Path(__file__).resolve().parents[2])
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (package_root, os.environ.get("PYTHONPATH", "")) if part
            )

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
            logger.warning("opencode-cli exited rc=%s stderr_bytes=%d", proc.returncode, len(stderr))
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
