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

from tusker_gateway.errors import BadRequestError, GatewayError, ProviderError, ProviderRouteDisabledError
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


# Worker responses that describe the request instead of the worker's health.
# ``tusker_gateway.kilo_worker`` returns 400 for its own request rejections
# (for example text-only content), so collapsing those into a generic 502
# would report a client mistake as a provider outage.
_WORKER_REQUEST_FAILURE_STATUSES = frozenset({400, 413, 415, 422, 428})


# Request-scoped tool manifests carry index-based MCP names (``gateway_tool_N``),
# so a shared warm server must pre-authorize that bounded name space. Requests
# with more tools than this fall back to the isolated per-request CLI path.
_WARM_TOOL_INDEX_LIMIT = 128
# The shared MCP bridge reads one active pointer, so warm tool requests run one
# at a time per worker; this keeps tool schemas and call channels request-scoped.
_WARM_TOOL_LOCK = asyncio.Lock()


def _worker_error(status: int, message: Any, code: Any) -> GatewayError:
    """Translate a Kilo worker HTTP failure into the client-visible error."""
    error_message = message if isinstance(message, str) and message.strip() else None
    error_code = code if isinstance(code, str) and code.strip() else None
    if status in _WORKER_REQUEST_FAILURE_STATUSES:
        error: GatewayError = BadRequestError(
            error_message or "Kilo worker request failed",
            code=error_code or "kilo_worker_failed",
        )
        error.status = status
        return error
    return ProviderError(
        error_message or "Kilo worker request failed",
        code=error_code or "kilo_worker_failed",
    )


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


def _point_broker(pointer: Path, manifest_file: Path, call_file: Path) -> None:
    """Atomically name the request the shared MCP bridge must serve."""
    temporary = pointer.with_name(f".{pointer.name}.{os.urandom(4).hex()}")
    temporary.write_text(
        json.dumps({"manifest": str(manifest_file), "call": str(call_file)}),
        encoding="utf-8",
    )
    os.replace(temporary, pointer)


def _clear_broker(pointer: Path) -> None:
    """Best-effort release of the shared bridge so no stale tools are served."""
    temporary = pointer.with_name(f".{pointer.name}.{os.urandom(4).hex()}")
    try:
        temporary.write_text("{}", encoding="utf-8")
        os.replace(temporary, pointer)
    except OSError:
        pass


async def _open_kilo_session(session: Any, server_url: str, directory: Path) -> str:
    """Create the per-request chat session on the warm Kilo server."""
    import aiohttp

    try:
        async with session.post(f"{server_url}/session", json={"directory": str(directory)}) as response:
            payload = await response.json()
    except (ValueError, aiohttp.ContentTypeError) as exc:
        raise ProviderError("Kilo warm server returned an invalid session", code="invalid_upstream_response") from exc
    session_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not session_id:
        raise ProviderError("Kilo warm server did not create a session", code="kilo_cli_failed")
    return session_id


async def _post_kilo_message(
    session: Any, server_url: str, session_id: str, body: dict[str, Any],
) -> dict[str, Any]:
    """Submit one prompt and return the completed turn (Kilo replies when done)."""
    import aiohttp

    try:
        async with session.post(f"{server_url}/session/{session_id}/message", json=body) as response:
            payload = await response.json()
    except (ValueError, aiohttp.ContentTypeError) as exc:
        raise ProviderError("Kilo warm server returned an invalid response", code="invalid_upstream_response") from exc
    if response.status >= 400 or not isinstance(payload, dict):
        raise ProviderError("Kilo warm server rejected the request", code="kilo_cli_failed")
    return payload


async def _close_kilo_session(session: Any, server_url: str, session_id: str) -> None:
    """Abort a running turn and delete the session so the server store stays bounded."""
    for method in ("post", "delete"):
        suffix = "/abort" if method == "post" else ""
        try:
            async with getattr(session, method)(f"{server_url}/session/{session_id}{suffix}") as response:
                await response.read()
        except Exception:  # cleanup must never fail the request it is cleaning up
            logger.debug("kilo warm session cleanup failed (%s)", method)


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
            if stream:
                return self._worker_stream_chat(
                    worker_url=worker_url, model=model, cli_model=cli_model,
                    messages=messages, tools=tools, tool_choice=tool_choice,
                )
            return await self._worker_chat(
                worker_url=worker_url,
                model=model,
                cli_model=cli_model,
                messages=messages,
                stream=stream,
                tools=tools,
                tool_choice=tool_choice,
            )
        warm_server_url = os.environ.get("TUSKER_KILO_WARM_SERVER_URL", "").strip()
        if warm_server_url and not stream and not tools:
            return await self._warm_chat(
                warm_server_url, cli_model, model, messages,
            )
        warm_tool_url = os.environ.get("TUSKER_KILO_WARM_TOOL_SERVER_URL", "").strip()
        broker_dir = os.environ.get("TUSKER_MCP_BROKER_DIR", "").strip()
        if warm_tool_url and broker_dir and tools:
            try:
                warm_tool_completion = await self._warm_tool_chat(
                    server_url=warm_tool_url,
                    broker_dir=broker_dir,
                    model=model,
                    cli_model=cli_model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                )
            except Exception as exc:  # degrade to the isolated CLI path, never fail the request
                logger.warning("kilo warm tool path unavailable (%s); using isolated CLI", type(exc).__name__)
                warm_tool_completion = None
            if warm_tool_completion is not None:
                return (
                    ClaudeCodeCLIAdapter._stream_completion(warm_tool_completion, stream)
                    if stream
                    else warm_tool_completion
                )
        executable = self._executable or os.environ.get("TUSKER_KILO_CLI_PATH") or "kilo"
        resolved = shutil.which(executable)

        if not resolved:
            raise ProviderError("Kilo CLI is not installed in the runtime", code="kilo_cli_unavailable")

        tool_manifest = _normalise_tools(tools, tool_choice)
        prompt = _prompt(
            messages, has_tools=bool(tool_manifest), tool_choice=tool_choice,
            provider_label="kilo-cli",
        )
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

        if stream:
            from tusker_gateway.provider_adapters.cli_streaming import stream_cli_jsonl, text_from_event

            temp_dir = Path(tempfile.mkdtemp(prefix="tusker-kilo-stream-"))
            call_file: Path | None = None
            config: dict[str, Any] = {"permission": {"*": "deny"}, "plugin": []}
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
            timeout = max(10.0, float(os.environ.get("TUSKER_KILO_CLI_TIMEOUT_SECS", "600")))
            return stream_cli_jsonl(
                command, env=env, prompt=prompt.encode(), model=model, timeout=timeout,
                text_extractor=text_from_event, call_file=call_file, cleanup_dir=temp_dir,
                error_code="kilo_cli_failed", timeout_message="Kilo CLI request timed out",
            )

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
            stderr_preview = stderr.decode("utf-8", errors="replace")[:512]
            logger.warning(
                "kilo-cli exited rc=%s stderr_bytes=%d stderr=%r",
                proc.returncode, len(stderr), stderr_preview,
            )
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

    async def _warm_chat(
        self,
        server_url: str,
        cli_model: str,
        public_model: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Use a persistent Kilo server for text-only requests.

        The small attach client still starts per request, but Kilo's model
        provider/session initialization stays warm. Tool requests deliberately
        use the isolated path above because their MCP manifest is request-scoped.
        """
        prompt = _prompt(messages, has_tools=False, tool_choice=None, provider_label="kilo-cli")
        executable = self._executable or os.environ.get("TUSKER_KILO_CLI_PATH") or "kilo"
        resolved = shutil.which(executable)
        if not resolved:
            raise ProviderError("Kilo CLI is not installed in the runtime", code="kilo_cli_unavailable")
        command = [resolved, "run", "--pure", "--format", "json", "--attach", server_url, "--model", cli_model]
        timeout = max(10.0, float(os.environ.get("TUSKER_KILO_CLI_TIMEOUT_SECS", "600")))
        started = asyncio.get_running_loop().time()
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
        except asyncio.TimeoutError as exc:
            await _stop_process(proc)
            raise ProviderError("Kilo warm request timed out", code="upstream_timeout") from exc
        logger.info("kilo warm request model=%s elapsed_s=%.3f rc=%s", cli_model, asyncio.get_running_loop().time() - started, proc.returncode)
        if proc.returncode != 0:
            logger.warning("kilo warm client failed rc=%s stderr=%r", proc.returncode, stderr.decode(errors="replace")[:512])
            raise ProviderError("Kilo warm request failed", code="kilo_cli_failed")
        text = self._extract_text(stdout)
        if text is None:
            raise ProviderError("Kilo warm request returned no completion", code="invalid_upstream_response")
        return {
            "id": f"chatcmpl-kilo-{os.urandom(8).hex()}",
            "object": "chat.completion",
            "model": public_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


    async def _warm_tool_chat(
        self,
        *,
        server_url: str,
        broker_dir: str,
        model: str,
        cli_model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
    ) -> dict[str, Any] | None:
        """Run a tool request on the warm server through the shared MCP bridge.

        Kilo lists MCP tools per session, so the bridge resolves this request's
        manifest from ``active.json``. Requests are serialized: the pointer names
        exactly one in-flight request, and the request's tool names are enabled
        explicitly through the session ``tools`` map.
        """
        import aiohttp

        tool_manifest = _normalise_tools(tools, tool_choice)
        if not tool_manifest or len(tool_manifest) > _WARM_TOOL_INDEX_LIMIT:
            return None
        provider_id, _, model_id = cli_model.partition("/")
        if not provider_id or not model_id:
            return None

        prompt = _prompt(
            messages, has_tools=True, tool_choice=tool_choice, provider_label="kilo-cli",
        )
        enabled = {f"gateway_{item['mcp_name']}": True for item in tool_manifest}
        timeout = max(10.0, float(os.environ.get("TUSKER_KILO_CLI_TIMEOUT_SECS", "600")))
        request_body = {
            "model": {"providerID": provider_id, "modelID": model_id},
            "agent": os.environ.get("TUSKER_KILO_WARM_AGENT", "code"),
            "tools": enabled,
            "parts": [{"type": "text", "text": prompt}],
        }

        async with _WARM_TOOL_LOCK:
            request_dir = Path(tempfile.mkdtemp(prefix="tusker-kilo-warm-"))
            manifest_file = request_dir / "tools.json"
            call_file = request_dir / "tool-call.json"
            manifest_file.write_text(json.dumps(tool_manifest), encoding="utf-8")
            pointer = Path(broker_dir) / "active.json"
            _point_broker(pointer, manifest_file, call_file)
            started = asyncio.get_running_loop().time()
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    session_id = await _open_kilo_session(session, server_url, request_dir)
                    message_task = asyncio.create_task(
                        _post_kilo_message(session, server_url, session_id, request_body),
                    )
                    call_task = asyncio.create_task(_read_tool_call(call_file, message_task))
                    tool_call: dict[str, Any] | None = None
                    payload: dict[str, Any] | None = None
                    try:
                        tool_call = await asyncio.wait_for(asyncio.shield(call_task), timeout)
                        if message_task.done() and not message_task.cancelled():
                            payload = message_task.result()
                    except asyncio.TimeoutError as exc:
                        message_task.cancel()
                        call_task.cancel()
                        raise ProviderError(
                            "Kilo warm tool request timed out", code="upstream_timeout",
                        ) from exc
                    except asyncio.CancelledError:
                        message_task.cancel()
                        call_task.cancel()
                        raise
                    finally:
                        await _close_kilo_session(session, server_url, session_id)

                    elapsed = asyncio.get_running_loop().time() - started
                    if tool_call is not None:
                        if (
                            not isinstance(tool_call.get("name"), str)
                            or not isinstance(tool_call.get("arguments"), dict)
                        ):
                            raise ProviderError(
                                "Kilo returned an invalid tool request",
                                code="invalid_upstream_response",
                            )
                        logger.info("kilo warm tool call model=%s elapsed_s=%.3f", cli_model, elapsed)
                        return ClaudeCodeCLIAdapter._tool_completion(model, tool_call)
                    if payload is None:
                        raise ProviderError(
                            "Kilo warm server returned no completion",
                            code="invalid_upstream_response",
                        )
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    if info.get("error"):
                        raise ProviderError("Kilo warm turn failed", code="kilo_cli_failed")
                    parts = payload.get("parts")
                    text = "".join(
                        part["text"]
                        for part in parts
                        if isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                    ) if isinstance(parts, list) else ""
                    if not text:
                        raise ProviderError(
                            "Kilo warm server returned no assistant response",
                            code="invalid_upstream_response",
                        )
                    logger.info("kilo warm text model=%s elapsed_s=%.3f", cli_model, elapsed)
                    return {
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
            finally:
                shutil.rmtree(request_dir, ignore_errors=True)
                _clear_broker(pointer)

    async def _worker_chat(
        self, *, worker_url: str, model: str, cli_model: str,
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
                        raise _worker_error(
                            response.status,
                            error.get("message") if isinstance(error, dict) else None,
                            error.get("code") if isinstance(error, dict) else None,
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
    def _worker_stream_chat(
        *, worker_url: str, model: str, cli_model: str,
        messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
        tool_choice: Any,
    ):
        """Keep the worker HTTP stream open and relay its OpenAI SSE bytes."""
        import aiohttp

        async def events():
            timeout = max(10.0, float(os.environ.get("TUSKER_KILO_CLI_TIMEOUT_SECS", "600")))
            request = {
                "model": cli_model, "messages": messages, "tools": tools,
                "tool_choice": tool_choice, "stream": True, "public_model": model,
            }
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as session:
                    async with session.post(
                        urljoin(worker_url.rstrip("/") + "/", "v1/chat/completions"),
                        json=request,
                    ) as response:
                        if response.status >= 400:
                            try:
                                payload = await response.json()
                            except (ValueError, aiohttp.ContentTypeError):
                                payload = {}
                            error = payload.get("error", {}) if isinstance(payload, dict) else {}
                            raise _worker_error(
                                response.status,
                                error.get("message") if isinstance(error, dict) else None,
                                error.get("code") if isinstance(error, dict) else None,
                            )
                        async for chunk in response.content.iter_any():
                            if chunk:
                                yield chunk
            except asyncio.TimeoutError as exc:
                raise ProviderError("Kilo worker request timed out", code="upstream_timeout") from exc
            except aiohttp.ClientError as exc:
                logger.warning("kilo worker stream unavailable: %s", type(exc).__name__)
                raise ProviderError("Kilo worker is unavailable", code="kilo_worker_unavailable") from exc

        return events()

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
