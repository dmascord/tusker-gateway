from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
from tusker_gateway.errors import BadRequestError, ProviderError, ProviderRouteDisabledError
from tusker_gateway.provider_adapters import provider_adapters
from tusker_gateway.provider_adapters.opencode_cli import OpenCodeCLIAdapter, _model_for_cli


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 34567

    async def communicate(self, _input: bytes):
        return self.stdout, self.stderr

    async def wait(self):
        return self.returncode


def _tool(name: str = "report_value"):
    return {"type": "function", "function": {
        "name": name,
        "description": "Return a value to the caller without executing it.",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
    }}


def test_opencode_cli_is_registered_as_non_zdr_local_provider():
    assert provider_adapters.get("opencode-cli") is not None
    provider = DEFAULT_PROVIDER_REGISTRY["opencode-cli"]
    assert provider.kind == "local"
    assert provider.zdr_ok is False


@pytest.mark.parametrize("value,expected", [
    ("big-pickle", "opencode/big-pickle"),
    ("opencode/big-pickle", "opencode/big-pickle"),
])
def test_model_name_maps_only_to_opencode_catalog(value, expected):
    assert _model_for_cli(value) == expected


@pytest.mark.parametrize("value", ["", "other-provider/model", "opencode/../model", "opencode/a/b", "-bad"])
def test_model_name_rejects_other_providers_and_invalid_identifiers(value):
    with pytest.raises(BadRequestError):
        _model_for_cli(value)


@pytest.mark.asyncio
async def test_route_is_opt_in(monkeypatch):
    monkeypatch.delenv("TUSKER_OPENCODE_CLI_ENABLED", raising=False)
    with pytest.raises(ProviderRouteDisabledError):
        await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle", messages=[],
        )


@pytest.mark.asyncio
async def test_text_result_parses_opencode_json_event_stream(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "test-zen-key")
    stdout = b'\n'.join([
        json.dumps({"type": "step_start", "part": {}}).encode(),
        json.dumps({"type": "text", "part": {"text": "hello "}}).encode(),
        json.dumps({"type": "text", "part": {"text": "world"}}).encode(),
    ])
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="/opt/opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(stdout))) as spawn:
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle", messages=[{"role": "user", "content": "hi"}],
        )
    assert result["choices"][0]["message"]["content"] == "hello world"
    args = spawn.await_args.args
    assert args[0] == "/opt/opencode"
    assert args[args.index("--model") + 1] == "opencode/big-pickle"
    assert "--standalone" in args
    env = spawn.await_args.kwargs["env"]
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "true"
    assert "OPENCODE_API_KEY" not in env
    assert "OPENCODE_ZEN_API_KEY" not in env
    assert "OPENCODE_CONFIG_CONTENT" not in env
    assert spawn.await_args.kwargs["cwd"]


@pytest.mark.asyncio
async def test_explicit_opencode_api_key_is_forwarded_without_aliasing_zen_key(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    monkeypatch.setenv("OPENCODE_API_KEY", "explicit-cli-key")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "gateway-provider-key")
    stdout = json.dumps({"type": "text", "part": {"text": "ok"}}).encode()
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="/opt/opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(stdout))) as spawn:
        await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle", messages=[{"role": "user", "content": "hi"}],
        )
    env = spawn.await_args.kwargs["env"]
    assert env["OPENCODE_API_KEY"] == "explicit-cli-key"
    assert "OPENCODE_ZEN_API_KEY" not in env


@pytest.mark.asyncio
async def test_mcp_tool_invocation_becomes_openai_tool_call(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "1")

    class ToolCallProcess(_FakeProcess):
        async def communicate(self, _input: bytes):
            env = json.loads(spawn.await_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
            bridge = env["mcp"]["gateway"]["environment"]
            Path(bridge["TUSKER_MCP_CALL_FILE"]).write_text(json.dumps({
                "id": "call_opencode_1", "name": "report_value", "arguments": {"value": "ok"},
            }))
            return b"", b""

    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=ToolCallProcess(b""))) as spawn:
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle", messages=[{"role": "user", "content": "report"}],
            tools=[_tool()], tool_choice="required",
        )

    call = result["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_opencode_1"
    assert call["function"]["name"] == "report_value"
    assert json.loads(call["function"]["arguments"]) == {"value": "ok"}
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    config = json.loads(spawn.await_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
    assert "permission" not in config
    assert config["mcp"]["gateway"]["type"] == "local"


@pytest.mark.asyncio
async def test_cli_error_and_missing_binary_are_actionable(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    adapter = OpenCodeCLIAdapter()
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value=None):
        with pytest.raises(ProviderError) as exc:
            await adapter.chat(provider="opencode-cli", model="big-pickle", messages=[])
    assert exc.value.code == "opencode_cli_unavailable"

    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(b"not json"))):
        with pytest.raises(ProviderError) as exc:
            await adapter.chat(provider="opencode-cli", model="big-pickle", messages=[])
    assert exc.value.code == "invalid_upstream_response"
