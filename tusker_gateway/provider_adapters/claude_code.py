"""Conservative adapter for Anthropic's official Claude Code CLI.

The adapter uses the runtime's existing Claude Code login without reading,
copying, refreshing, or persisting credentials. Built-in CLI tools are off;
only request-scoped MCP proxies for the client's declared tools are exposed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any

from tusker_gateway.errors import (
    BadRequestError,
    ClaudeAuthRequiredError,
    ProviderError,
    ProviderRouteDisabledError,
)
from tusker_gateway.sse import format_openai_chunk, sse_done, sse_frame

logger = logging.getLogger(__name__)

_MODEL_ALIASES = {
    "sonnet": "sonnet",
    "opus": "opus",
    "haiku": "haiku",
}


def _cli_env() -> dict[str, str]:
    """Build the intentionally small environment shared by Claude CLI calls."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/home/tusker"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "USER": os.environ.get("USER", ""),
        "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "")),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "DISABLE_AUTOUPDATER": "1",
    }
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    return env


async def claude_auth_status(*, executable: str | None = None) -> dict[str, str]:
    """Return a sanitized status; never expose or log Claude's raw auth JSON."""
    path = executable or os.environ.get("TUSKER_CLAUDE_CODE_PATH") or "claude"
    resolved = shutil.which(path)
    if not resolved:
        return {"status": "unknown"}
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved, "auth", "status", "--json",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_cli_env(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return {"status": "unknown"}
        value = json.loads(stdout)
        if not isinstance(value, dict) or not isinstance(value.get("loggedIn"), bool):
            return {"status": "unknown"}
        if not value["loggedIn"]:
            return {"status": "login_required"}
        status = {"status": "authenticated"}
        method = value.get("authMethod")
        if isinstance(method, str) and method in {"claude.ai", "console", "third-party"}:
            status["auth_method"] = method
        return status
    except (asyncio.TimeoutError, OSError, ValueError, TypeError):
        return {"status": "unknown"}


def _auth_error_indicated(*parts: bytes | str) -> bool:
    text = " ".join(
        part.decode("utf-8", errors="ignore") if isinstance(part, bytes) else part
        for part in parts
    ).lower()
    return any(phrase in text for phrase in (
        "login expired", "please run /login", "not logged in",
        "authentication required", "oauth token expired", "token has expired",
    ))


def _enabled() -> bool:
    return os.environ.get("TUSKER_CLAUDE_CODE_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                raise BadRequestError(
                    "claude-code-cli currently accepts text-only message content",
                    code="unsupported_message_content",
                )
        return "\n".join(parts)
    raise BadRequestError("Message content must be text", code="invalid_message_content")


def _normalise_tools(tools: list[dict[str, Any]] | None, tool_choice: Any) -> list[dict[str, Any]]:
    from tusker_gateway.tool_formats import normalize_tools

    normalized = normalize_tools(tools)
    if tool_choice == "none":
        return []
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        function = tool_choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        normalized = [
            item for item in normalized
            if item["function"]["name"] == name
        ]
        if not normalized:
            raise BadRequestError("tool_choice references an undeclared function", code="invalid_tool_choice")
    if tool_choice == "required" and not normalized:
        raise BadRequestError("tool_choice requires tools, but none were supplied", code="invalid_tool_choice")
    result = []
    for index, item in enumerate(normalized):
        function = item["function"]
        parameters = function.get("parameters")
        if (
            not isinstance(parameters, dict)
            or parameters.get("type", "object") != "object"
            or not isinstance(parameters.get("properties", {}), dict)
        ):
            raise BadRequestError(
                f"Tool {function['name']!r} must have an object JSON schema",
                code="invalid_tool_schema",
            )
        result.append({
            "mcp_name": f"gateway_tool_{index}",
            "name": function["name"],
            "description": function.get("description", ""),
            "input_schema": parameters,
        })
    return result


def _prompt(messages: list[dict[str, Any]], *, has_tools: bool, tool_choice: Any) -> str:
    from tusker_gateway.tool_formats import normalize_tool_calls

    supported = {"system", "developer", "user", "assistant"}
    transcript: list[str] = []
    for message in messages:
        role = str(message.get("role", ""))
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            content = _as_text(message.get("content", ""))
            transcript.append(f"<gateway_tool_result call_id={json.dumps(call_id)}>\n{content}\n</gateway_tool_result>")
            continue
        if role not in supported:
            raise BadRequestError(
                f"claude-code-cli does not support {role!r} messages yet",
                code="unsupported_message_role",
            )
        content = _as_text(message.get("content", ""))
        if content:
            transcript.append(f"<{role}>\n{content}\n</{role}>")
        calls = message.get("tool_calls")
        if calls is None and message.get("function_call") is not None:
            calls = [message["function_call"]]
        for call in normalize_tool_calls(calls):
            transcript.append(
                "<gateway_tool_call>\n"
                + json.dumps(call, ensure_ascii=False, separators=(",", ":"))
                + "\n</gateway_tool_call>"
            )
    if has_tools:
        if tool_choice == "required":
            transcript.append(
                "<gateway_tool_policy>Call one of the supplied gateway tools now. "
                "Return a tool call instead of answering in prose.</gateway_tool_policy>"
            )
        elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            name = tool_choice.get("function", {}).get("name")
            transcript.append(
                f"<gateway_tool_policy>Call the required tool {name!r}.</gateway_tool_policy>"
            )
    return "\n\n".join(transcript)


async def _read_tool_call(call_file: Path, process_done: asyncio.Task) -> dict[str, Any] | None:
    while not process_done.done():
        try:
            value = json.loads(call_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            await asyncio.sleep(0.03)
    try:
        value = json.loads(call_file.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), 3)
    except (ProcessLookupError, asyncio.TimeoutError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


class ClaudeCodeCLIAdapter:
    """Invoke Claude Code with request-scoped MCP proxies for client tools."""

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
        alias = model.rsplit("/", 1)[-1].lower()
        cli_model = _MODEL_ALIASES.get(alias)
        if cli_model is None:
            raise BadRequestError(
                "claude-code-cli model must be one of sonnet, opus, or haiku",
                code="unsupported_model",
            )
        executable = self._executable or os.environ.get("TUSKER_CLAUDE_CODE_PATH") or "claude"
        resolved = shutil.which(executable)
        if not resolved:
            raise ProviderError(
                "Claude Code CLI is not installed in the gateway runtime",
                code="claude_code_cli_unavailable",
            )

        tool_manifest = _normalise_tools(tools, tool_choice)
        prompt = _prompt(messages, has_tools=bool(tool_manifest), tool_choice=tool_choice)
        # Keep the child environment allowlisted (no gateway/provider secrets).
        env = _cli_env()
        with tempfile.TemporaryDirectory(prefix="tusker-claude-") as temp_name:
            temp_dir = Path(temp_name)
            command = [
                resolved, "-p", "--output-format", "json", "--model", cli_model,
                "--tools", "", "--permission-mode",
                "dontAsk" if tool_manifest else "plan",
                "--permission-prompts", "none",
                "--no-session-persistence",
            ]
            call_file: Path | None = None
            if tool_manifest:
                manifest_file = temp_dir / "tools.json"
                call_file = temp_dir / "tool-call.json"
                mcp_config = temp_dir / "mcp.json"
                manifest_file.write_text(json.dumps(tool_manifest), encoding="utf-8")
                mcp_env = {
                    "TUSKER_MCP_MANIFEST": str(manifest_file),
                    "TUSKER_MCP_CALL_FILE": str(call_file),
                }
                mcp_config.write_text(json.dumps({
                    "mcpServers": {
                        "gateway": {
                            "command": sys.executable,
                            "args": ["-m", "tusker_gateway.provider_adapters.mcp_stdio"],
                            "env": mcp_env,
                        },
                    },
                }), encoding="utf-8")
                command.extend([
                    "--mcp-config", str(mcp_config), "--strict-mcp-config",
                    "--allowedTools", "mcp__gateway__*",
                ])
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
                timeout = max(
                    10.0,
                    float(os.environ.get("TUSKER_CLAUDE_CODE_TIMEOUT_SECS", "600")),
                )
                tool_call = None
                if call_file is not None:
                    call_task = asyncio.create_task(_read_tool_call(call_file, communicate_task))
                    try:
                        done, _ = await asyncio.wait(
                            {communicate_task, call_task},
                            timeout=timeout,
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
                    if (
                        not isinstance(tool_call.get("name"), str)
                        or not isinstance(tool_call.get("arguments"), dict)
                    ):
                        raise ProviderError(
                            "Claude Code CLI returned an invalid tool request",
                            code="invalid_upstream_response",
                        )
                    completion = self._tool_completion(model, tool_call)
                    return self._stream_completion(completion, stream) if stream else completion
                stdout, stderr = await communicate_task
            except asyncio.TimeoutError as exc:
                raise ProviderError(
                    "Claude Code CLI request timed out",
                    code="upstream_timeout",
                ) from exc
            except asyncio.CancelledError:
                raise
            except ProviderError:
                raise
            except Exception as exc:
                logger.warning("claude-code-cli spawn failed: %s", type(exc).__name__)
                raise ProviderError(
                    "Could not start Claude Code CLI",
                    code="claude_code_cli_failed",
                ) from exc

        if proc.returncode != 0:
            logger.warning(
                "claude-code-cli exited rc=%s stderr_bytes=%d",
                proc.returncode,
                len(stderr),
            )
            if _auth_error_indicated(stdout, stderr):
                raise ClaudeAuthRequiredError()
            auth_status = await claude_auth_status(executable=resolved)
            if auth_status["status"] == "login_required":
                raise ClaudeAuthRequiredError()
            raise ProviderError("Claude Code CLI request failed", code="claude_code_cli_failed")
        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(
                "Claude Code CLI returned malformed JSON",
                code="invalid_upstream_response",
            ) from exc
        if not isinstance(result, dict) or not isinstance(result.get("result"), str):
            raise ProviderError(
                "Claude Code CLI response did not contain a result",
                code="invalid_upstream_response",
            )
        if result.get("is_error"):
            api_status = result.get("api_error_status")
            logger.warning(
                "claude-code-cli reported an error subtype=%s api_status=%s",
                result.get("subtype"),
                api_status,
            )
            if api_status == 401 or _auth_error_indicated(result.get("result", "")):
                raise ClaudeAuthRequiredError()
            auth_status = await claude_auth_status(executable=resolved)
            if auth_status["status"] == "login_required":
                raise ClaudeAuthRequiredError()
            raise ProviderError(
                "Claude Code CLI could not complete the request; check its local login and account status",
                code="claude_code_cli_failed",
            )
        text = result["result"]
        usage = result.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        completion = {
            "id": str(result.get("session_id") or "claude-code-cli"),
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        }
        return self._stream_completion(completion, stream) if stream else completion

    @staticmethod
    def _tool_completion(model: str, tool_call: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{str(tool_call['id']).removeprefix('call_')}",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": str(tool_call["id"]),
                        "type": "function",
                        "function": {
                            "name": tool_call["name"],
                            "arguments": json.dumps(
                                tool_call["arguments"], ensure_ascii=False, separators=(",", ":")
                            ),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    @staticmethod
    def _stream_completion(completion: dict[str, Any], stream: bool) -> Any:
        if not stream:
            return completion

        async def events():
            choice = completion["choices"][0]
            message = choice["message"]
            if message.get("tool_calls"):
                call = message["tool_calls"][0]
                chunk = {
                    "id": completion["id"],
                    "object": "chat.completion.chunk",
                    "model": completion["model"],
                    "choices": [{"index": 0, "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["function"]["name"],
                                "arguments": call["function"]["arguments"],
                            },
                        }],
                    }, "finish_reason": None}],
                }
                yield sse_frame(chunk)
                yield sse_frame({
                    **chunk,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                })
            else:
                chunk = format_openai_chunk(
                    content=message.get("content", ""), model=completion["model"]
                )
                yield sse_frame(chunk)
                yield sse_frame({
                    **chunk,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                })
            yield sse_done()

        return events()
