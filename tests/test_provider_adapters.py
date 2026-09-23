from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
from tusker_gateway.errors import BadRequestError, ProviderError, ProviderRouteDisabledError
from tusker_gateway.provider_adapters import ProviderAdapterRegistry, provider_adapters
from tusker_gateway.provider_adapters.claude_code import ClaudeCodeCLIAdapter
from tusker_gateway.passthrough import _configured_endpoint
from tusker_gateway.sse import sse_data_payload


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 23456

    async def communicate(self, _input: bytes):
        return self.stdout, self.stderr

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_claude_cli_is_registered_as_local_provider():
    assert provider_adapters.get("claude-code-cli") is not None
    config_provider = DEFAULT_PROVIDER_REGISTRY["claude-code-cli"]
    assert config_provider.kind == "local"
    assert config_provider.zdr_ok is False
    assert _configured_endpoint({"providers": DEFAULT_PROVIDER_REGISTRY}, "claude-code-cli")[
        "base_url"
    ] == "claude://local"


def test_adapter_registry_normalizes_provider_names():
    registry = ProviderAdapterRegistry()
    adapter = object()
    registry.register("Test_Adapter", adapter)
    assert registry.get("test-adapter") is adapter
    with pytest.raises(ValueError):
        registry.register("  ", adapter)


def test_tool_choice_filters_exposed_functions():
    from tusker_gateway.provider_adapters.claude_code import _normalise_tools

    tools = [
        {"type": "function", "function": {"name": "one"}},
        {"type": "function", "function": {"name": "two"}},
    ]
    selected = _normalise_tools(tools, {"type": "function", "function": {"name": "two"}})
    assert len(selected) == 1
    assert selected[0]["name"] == "two"
    assert _normalise_tools(tools, "none") == []
    with pytest.raises(BadRequestError, match="requires tools"):
        _normalise_tools(None, "required")


@pytest.mark.asyncio
async def test_cli_route_is_disabled_unless_opted_in(monkeypatch):
    monkeypatch.delenv("TUSKER_CLAUDE_CODE_ENABLED", raising=False)
    with pytest.raises(ProviderRouteDisabledError):
        await ClaudeCodeCLIAdapter(executable="claude").chat(
            provider="claude-code-cli", model="sonnet", messages=[], stream=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-code-cli/unknown", "claude-code-cli/sonnet-latest"])
async def test_cli_rejects_unallowlisted_model_aliases(monkeypatch, model):
    monkeypatch.setenv("TUSKER_CLAUDE_CODE_ENABLED", "true")
    with pytest.raises(BadRequestError) as exc:
        await ClaudeCodeCLIAdapter().chat(
            provider="claude-code-cli", model=model, messages=[], stream=False,
        )
    assert exc.value.code == "unsupported_model"


@pytest.mark.asyncio
async def test_cli_returns_client_tool_call_without_executing_it(monkeypatch):
    monkeypatch.setenv("TUSKER_CLAUDE_CODE_ENABLED", "true")
    tool_call = {
        "id": "call_123",
        "name": "bash",
        "arguments": {"command": "echo hi"},
    }

    class ToolCallProcess(_FakeProcess):
        async def communicate(self, _input: bytes):
            config_path = spawn.await_args.args[spawn.await_args.args.index("--mcp-config") + 1]
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            bridge_env = config["mcpServers"]["gateway"]["env"]
            Path(bridge_env["TUSKER_MCP_CALL_FILE"]).write_text(json.dumps(tool_call))
            return b"", b""

    process = ToolCallProcess(b"")
    with patch("tusker_gateway.provider_adapters.claude_code.shutil.which", return_value="claude"), \
         patch("tusker_gateway.provider_adapters.claude_code.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=process)) as spawn:
        result = await ClaudeCodeCLIAdapter().chat(
            provider="claude-code-cli", model="sonnet", messages=[], stream=False,
            tools=[{"type": "function", "function": {"name": "bash"}}],
        )
    call = result["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_123"
    assert call["function"]["name"] == "bash"
    assert json.loads(call["function"]["arguments"]) == {"command": "echo hi"}
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert "--strict-mcp-config" in spawn.await_args.args
    assert spawn.await_args.args[spawn.await_args.args.index("--tools") + 1] == ""
    assert spawn.await_args.args[spawn.await_args.args.index("--permission-mode") + 1] == "dontAsk"
    assert spawn.await_args.args[spawn.await_args.args.index("--allowedTools") + 1] == "mcp__gateway__*"


@pytest.mark.asyncio
async def test_cli_rejects_non_text_but_replays_tool_history(monkeypatch):
    monkeypatch.setenv("TUSKER_CLAUDE_CODE_ENABLED", "true")
    adapter = ClaudeCodeCLIAdapter()
    with pytest.raises(BadRequestError, match="text-only"):
        await adapter.chat(
            provider="claude-code-cli", model="sonnet",
            messages=[{"role": "user", "content": [{"type": "image_url"}]}], stream=False,
        )
    from tusker_gateway.provider_adapters.claude_code import _prompt
    prompt = _prompt(
        [
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
              "function": {"name": "lookup", "arguments": "{}"}}], "content": None},
            {"role": "tool", "tool_call_id": "c1", "content": "found"},
        ], has_tools=True, tool_choice="auto",
    )
    assert "gateway_tool_call" in prompt
    assert "gateway_tool_result" in prompt


@pytest.mark.asyncio
async def test_cli_invokes_allowlisted_model_and_converts_result(monkeypatch):
    monkeypatch.setenv("TUSKER_CLAUDE_CODE_ENABLED", "1")
    payload = {"session_id": "s1", "result": "hello", "usage": {"input_tokens": 12,
                                                                      "output_tokens": 4}}
    proc = _FakeProcess(json.dumps(payload).encode())
    with patch("tusker_gateway.provider_adapters.claude_code.shutil.which", return_value="/bin/claude"), \
         patch("tusker_gateway.provider_adapters.claude_code.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=proc)) as spawn:
        result = await ClaudeCodeCLIAdapter().chat(
            provider="claude-code-cli", model="claude-code-cli/sonnet",
            messages=[{"role": "user", "content": "hi"}], stream=False,
        )
    assert result["choices"][0]["message"]["content"] == "hello"
    assert result["usage"] == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    args = spawn.await_args.args
    assert args[0] == "/bin/claude"
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--tools") + 1] == ""
    assert "--no-session-persistence" in args
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in spawn.await_args.kwargs["env"]
    assert "USER" in spawn.await_args.kwargs["env"]
    assert "TMPDIR" in spawn.await_args.kwargs["env"]


@pytest.mark.asyncio
async def test_cli_stream_result_uses_openai_sse_and_done(monkeypatch):
    monkeypatch.setenv("TUSKER_CLAUDE_CODE_ENABLED", "true")
    proc = _FakeProcess(b'{"result":"streamed","usage":{}}')
    with patch("tusker_gateway.provider_adapters.claude_code.shutil.which", return_value="claude"), \
         patch("tusker_gateway.provider_adapters.claude_code.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=proc)):
        result = await ClaudeCodeCLIAdapter().chat(
            provider="claude-code-cli", model="haiku", messages=[], stream=True,
        )
    frames = [frame async for frame in result]
    assert json.loads(sse_data_payload(frames[0]))["choices"][0]["delta"]["content"] == "streamed"
    assert json.loads(sse_data_payload(frames[1]))["choices"][0]["finish_reason"] == "stop"
    assert frames[2] == b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_cli_stream_tool_call_preserves_openai_tool_delta(monkeypatch):
    monkeypatch.setenv("TUSKER_CLAUDE_CODE_ENABLED", "true")

    class ToolCallProcess(_FakeProcess):
        async def communicate(self, _input: bytes):
            config_path = spawn.await_args.args[spawn.await_args.args.index("--mcp-config") + 1]
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            bridge_env = config["mcpServers"]["gateway"]["env"]
            Path(bridge_env["TUSKER_MCP_CALL_FILE"]).write_text(json.dumps({
                "id": "call_stream", "name": "ask", "arguments": {"q": "ok"},
            }))
            return b"", b""

    with patch("tusker_gateway.provider_adapters.claude_code.shutil.which", return_value="claude"), \
         patch("tusker_gateway.provider_adapters.claude_code.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=ToolCallProcess(b""))) as spawn:
        result = await ClaudeCodeCLIAdapter().chat(
            provider="claude-code-cli", model="sonnet", messages=[], stream=True,
            tools=[{"type": "function", "function": {"name": "ask"}}],
        )
    frames = [frame async for frame in result]
    delta = json.loads(sse_data_payload(frames[0]))["choices"][0]["delta"]["tool_calls"][0]
    assert delta["id"] == "call_stream"
    assert delta["function"]["name"] == "ask"
    assert json.loads(delta["function"]["arguments"]) == {"q": "ok"}
    assert json.loads(sse_data_payload(frames[1]))["choices"][0]["finish_reason"] == "tool_calls"


def test_mcp_stdio_advertises_and_publishes_client_tools(monkeypatch, tmp_path):
    from tusker_gateway.provider_adapters import mcp_stdio

    manifest_path = tmp_path / "manifest.json"
    call_path = tmp_path / "call.json"
    manifest_path.write_text(json.dumps([{
        "mcp_name": "gateway_tool_0", "name": "ask", "description": "Ask user",
        "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}},
    }]))
    monkeypatch.setenv("TUSKER_MCP_MANIFEST", str(manifest_path))
    monkeypatch.setenv("TUSKER_MCP_CALL_FILE", str(call_path))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "gateway_tool_0", "arguments": {"question": "Proceed?"},
        }},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "gateway_tool_0", "arguments": {"question": "overwrite?"},
        }},
    ]
    monkeypatch.setattr(mcp_stdio.sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests)) + "\n"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        mcp_stdio.main()
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["result"]["protocolVersion"] == "2025-03-26"
    assert responses[1]["result"]["tools"][0]["name"] == "gateway_tool_0"
    assert "returned to the connected client" in responses[2]["result"]["content"][0]["text"]
    assert responses[3]["error"]["code"] == -32603
    published = json.loads(call_path.read_text())
    assert published["name"] == "ask"
    assert published["arguments"] == {"question": "Proceed?"}


@pytest.mark.asyncio
async def test_tool_completion_passes_gateway_contract_validation():
    from tusker_gateway.endpoints import _prepare_stream_result

    tools = [{"type": "function", "function": {
        "name": "report_value",
        "parameters": {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        },
    }}]
    completion = ClaudeCodeCLIAdapter._tool_completion("sonnet", {
        "id": "call_abc", "name": "report_value", "arguments": {"value": "ok"},
    })
    prepared = await _prepare_stream_result(
        completion,
        provider="claude-code-cli",
        model="sonnet",
        request_id="req_adapter_test",
        tools_requested=True,
        tools=tools,
        tool_choice="required",
    )
    assert prepared["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "report_value"


@pytest.mark.asyncio
async def test_passthrough_dispatches_adapter_without_http(monkeypatch):
    from tusker_gateway.passthrough import PassthroughClient

    adapter = AsyncMock()
    adapter.chat = AsyncMock(return_value={"choices": []})
    client = object.__new__(PassthroughClient)
    client._config = {}
    client._http = AsyncMock()
    monkeypatch.setattr(provider_adapters, "get", lambda _provider: adapter)
    result = await client.chat(
        "claude-code-cli", "sonnet", [{"role": "user", "content": "hello"}],
    )
    assert result == {"choices": []}
    adapter.chat.assert_awaited_once()
    client._http.request.assert_not_called()


@pytest.mark.asyncio
async def test_cli_reports_missing_binary_and_bad_json(monkeypatch):
    monkeypatch.setenv("TUSKER_CLAUDE_CODE_ENABLED", "true")
    adapter = ClaudeCodeCLIAdapter()
    with patch("tusker_gateway.provider_adapters.claude_code.shutil.which", return_value=None):
        with pytest.raises(ProviderError) as exc:
            await adapter.chat(provider="claude-code-cli", model="opus", messages=[], stream=False)
    assert exc.value.code == "claude_code_cli_unavailable"

    with patch("tusker_gateway.provider_adapters.claude_code.shutil.which", return_value="claude"), \
         patch("tusker_gateway.provider_adapters.claude_code.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(b"not-json"))):
        with pytest.raises(ProviderError) as exc:
            await adapter.chat(provider="claude-code-cli", model="opus", messages=[], stream=False)
    assert exc.value.code == "invalid_upstream_response"


@pytest.mark.asyncio
async def test_cli_reports_nonzero_exit_without_exposing_stderr_to_client(monkeypatch):
    monkeypatch.setenv("TUSKER_CLAUDE_CODE_ENABLED", "true")
    with patch("tusker_gateway.provider_adapters.claude_code.shutil.which", return_value="claude"), \
         patch("tusker_gateway.provider_adapters.claude_code.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(b"", b"private account detail", 1))):
        with pytest.raises(ProviderError) as exc:
            await ClaudeCodeCLIAdapter().chat(
                provider="claude-code-cli", model="opus", messages=[], stream=False,
            )
    assert "private account" not in exc.value.message
    assert exc.value.code == "claude_code_cli_failed"
