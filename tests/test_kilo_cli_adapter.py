from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
from tusker_gateway.errors import BadRequestError, ProviderError, ProviderRouteDisabledError
from tusker_gateway.provider_adapters import provider_adapters
from tusker_gateway.provider_adapters.kilo_cli import KiloCLIAdapter, _model_for_cli


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 34568

    async def communicate(self, _input: bytes):
        return self.stdout, self.stderr

    async def wait(self):
        return self.returncode


def _tool():
    return {"type": "function", "function": {
        "name": "report_value",
        "description": "Return a value without executing it.",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
    }}


def test_kilo_cli_registered_local_and_non_zdr():
    assert provider_adapters.get("kilo-cli") is not None
    provider = DEFAULT_PROVIDER_REGISTRY["kilo-cli"]
    assert provider.kind == "local"
    assert provider.zdr_ok is False


@pytest.mark.parametrize(("value", "expected"), [
    ("anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-4.6"),
    ("kilo-cli/anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-4.6"),
    ("openai/gpt-6-luna", "openai/gpt-6-luna"),
    ("groq/openai/gpt-oss-20b", "groq/openai/gpt-oss-20b"),
])
def test_kilo_model_identifier(value, expected):
    assert _model_for_cli(value) == expected


@pytest.mark.parametrize("value", [
    "", "anthropic", "a//c", "../model", "anthropic/../model",
    "anthropic/-flag", "other provider/model",
])
def test_kilo_model_rejects_invalid_ids(value):
    with pytest.raises(BadRequestError):
        _model_for_cli(value)


@pytest.mark.asyncio
async def test_kilo_adapter_is_opt_in(monkeypatch):
    monkeypatch.delenv("TUSKER_KILO_CLI_ENABLED", raising=False)
    with pytest.raises(ProviderRouteDisabledError):
        await KiloCLIAdapter().chat(provider="kilo-cli", model="anthropic/model", messages=[])


@pytest.mark.asyncio
async def test_kilo_text_request_uses_headless_json_cli(monkeypatch):
    monkeypatch.setenv("TUSKER_KILO_CLI_ENABLED", "true")
    stdout = b'\n'.join([
        json.dumps({"type": "step_start", "part": {}}).encode(),
        json.dumps({"type": "text", "part": {"text": "hello "}}).encode(),
        json.dumps({"type": "text", "part": {"text": "Kilo"}}).encode(),
    ])
    with patch("tusker_gateway.provider_adapters.kilo_cli.shutil.which", return_value="/opt/kilo"), \
         patch("tusker_gateway.provider_adapters.kilo_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(stdout))) as spawn:
        result = await KiloCLIAdapter().chat(
            provider="kilo-cli", model="anthropic/claude-sonnet-4.6",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert result["choices"][0]["message"]["content"] == "hello Kilo"
    args = spawn.await_args.args
    assert args[0] == "/opt/kilo"
    assert "--pure" in args
    assert args[1] == "run"
    assert args[args.index("--format") + 1] == "json"
    assert args[args.index("--model") + 1] == "anthropic/claude-sonnet-4.6"
    env = spawn.await_args.kwargs["env"]
    assert env["KILO_DISABLE_PROJECT_CONFIG"] == "true"
    assert "OPENCODE_CONFIG_CONTENT" not in env
    config = json.loads(env["KILO_CONFIG_CONTENT"])
    assert config["permission"]["*"] == "deny"
    assert config["plugin"] == []


@pytest.mark.asyncio
async def test_kilo_client_tools_are_only_exposed_as_request_scoped_mcp(monkeypatch):
    monkeypatch.setenv("TUSKER_KILO_CLI_ENABLED", "1")

    class ToolCallProcess(_FakeProcess):
        async def communicate(self, _input: bytes):
            config = json.loads(spawn.await_args.kwargs["env"]["KILO_CONFIG_CONTENT"])
            bridge = config["mcp"]["gateway"]["environment"]
            Path(bridge["TUSKER_MCP_CALL_FILE"]).write_text(json.dumps({
                "id": "call_kilo_1", "name": "report_value", "arguments": {"value": "ok"},
            }))
            return b"", b""

    with patch("tusker_gateway.provider_adapters.kilo_cli.shutil.which", return_value="kilo"), \
         patch("tusker_gateway.provider_adapters.kilo_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=ToolCallProcess(b""))) as spawn:
        result = await KiloCLIAdapter().chat(
            provider="kilo-cli", model="anthropic/model", messages=[{"role": "user", "content": "call"}],
            tools=[_tool()], tool_choice="required",
        )
    call = result["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_kilo_1"
    assert call["function"]["name"] == "report_value"
    assert json.loads(call["function"]["arguments"]) == {"value": "ok"}
    config = json.loads(spawn.await_args.kwargs["env"]["KILO_CONFIG_CONTENT"])
    assert config["permission"]["gateway_gateway_tool_0"] == "allow"
    assert config["mcp"]["gateway"]["type"] == "local"


@pytest.mark.asyncio
async def test_kilo_missing_binary_and_malformed_output_errors(monkeypatch):
    monkeypatch.setenv("TUSKER_KILO_CLI_ENABLED", "true")
    with patch("tusker_gateway.provider_adapters.kilo_cli.shutil.which", return_value=None):
        with pytest.raises(ProviderError) as exc:
            await KiloCLIAdapter().chat(provider="kilo-cli", model="anthropic/model", messages=[])
    assert exc.value.code == "kilo_cli_unavailable"

    with patch("tusker_gateway.provider_adapters.kilo_cli.shutil.which", return_value="kilo"), \
         patch("tusker_gateway.provider_adapters.kilo_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(b"not json"))):
        with pytest.raises(ProviderError) as exc:
            await KiloCLIAdapter().chat(provider="kilo-cli", model="anthropic/model", messages=[])
    assert exc.value.code == "invalid_upstream_response"
