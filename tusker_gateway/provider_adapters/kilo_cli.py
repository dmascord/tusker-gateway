"""Kilo Code CLI provider transport with isolated gateway tool proxies."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from urllib.parse import urljoin
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
        len(parts) < 2
        or any(not part for part in parts)
        or any(part in {".", ".."} or part.startswith("-") for part in parts)
        or any(not (ch.isalnum() or ch in "._-~") for part in parts for ch in part)
    ):
        raise BadRequestError(
            "kilo-cli model must be a provider/model identifier",
            code="unsupported_model",
        )
    return value


def _provider_api_key(model: str) -> tuple[str, str] | None:
    """Return only the selected model provider's configured key, if present."""
    provider = model.split("/", 1)[0].lower()
    try:
        from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY

        provider_config = DEFAULT_PROVIDER_REGISTRY.get(provider)
    except (ImportError, AttributeError):
        provider_config = None
    env_name = (
        provider_config.auth_env
        if provider_config is not None and provider_config.auth_env
        else f"{provider.upper().replace('-', '_')}_API_KEY"
    )
    source_names = (
        env_name,
        f"PROVIDER_{env_name}",
        f"PROVIDER_{provider.upper().replace('-', '_')}_API_KEY",
    )
    for source in dict.fromkeys(source_names):
        value = os.environ.get(source)
        if value:
            return env_name, value
    return None


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
        worker_url = os.environ.get("TUSKER_KILO_WORKER_URL", "").strip()
        if worker_url:
            return await self._worker_chat(
                worker_url=worker_url,
                model=model,
                cli_model=cli_model,
                messages=messages,
                stream=stream,
                tools=tools,
                tool_choice=tool_choice,
            )
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
        # Kilo supports direct provider credentials through their conventional
        # environment names. Pass only the key corresponding to the explicit
        # provider in this model ID, never the gateway's entire key set.
        selected_key = _provider_api_key(cli_model)
        if selected_key:
            env[selected_key[0]] = selected_key[1]
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
    async def _worker_chat(
        *, worker_url: str, model: str, cli_model: str,
        messages: list[dict[str, Any]], stream: bool,
        tools: list[dict[str, Any]] | None, tool_choice: Any,
    ) -> dict[str, Any] | Any:
        """Forward a normalized request to the isolated in-cluster Kilo worker."""
        import aiohttp

        timeout = max(10.0, float(os.environ.get("TUSKER_KILO_CLI_TIMEOUT_SECS", "600")))
        request = {
            "model": cli_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    urljoin(worker_url.rstrip("/") + "/", "v1/chat/completions"),
                    json=request,
                ) as response:
                    try:
                        payload = await response.json()
                    except (ValueError, aiohttp.ContentTypeError) as exc:
                        raise ProviderError(
                            "Kilo worker returned an invalid response",
                            code="invalid_upstream_response",
                        ) from exc
                    if response.status >= 400:
                        error = payload.get("error", {}) if isinstance(payload, dict) else {}
                        code = error.get("code") if isinstance(error, dict) else None
                        message = error.get("message") if isinstance(error, dict) else None
                        raise ProviderError(
                            message or "Kilo worker request failed",
                            code=code or "kilo_worker_failed",
                        )
        except asyncio.TimeoutError as exc:
            raise ProviderError("Kilo worker request timed out", code="upstream_timeout") from exc
        except aiohttp.ClientError as exc:
            logger.warning("kilo worker unavailable: %s", type(exc).__name__)
            raise ProviderError("Kilo worker is unavailable", code="kilo_worker_unavailable") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
            raise ProviderError("Kilo worker returned an invalid completion", code="invalid_upstream_response")
        payload["model"] = model
        return ClaudeCodeCLIAdapter._stream_completion(payload, stream) if stream else payload

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
