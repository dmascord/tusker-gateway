"""Warm Kilo tool path: shared MCP broker routing and isolated-CLI fallback."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.errors import ProviderError
from tusker_gateway.provider_adapters.kilo_cli import _WARM_TOOL_INDEX_LIMIT, KiloCLIAdapter


def _tool() -> dict:
    return {"type": "function", "function": {
        "name": "report_value",
        "description": "Return a value without executing it.",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
    }}


def test_mcp_bridge_resolves_request_paths_from_broker_pointer(monkeypatch, tmp_path):
    from tusker_gateway.provider_adapters import mcp_stdio

    manifest = tmp_path / "tools.json"
    manifest.write_text(json.dumps([{
        "mcp_name": "gateway_tool_0",
        "name": "report_value",
        "description": "Return a value without executing it.",
        "input_schema": {"type": "object"},
    }]), encoding="utf-8")
    monkeypatch.setenv("TUSKER_MCP_BROKER_DIR", str(tmp_path))
    # The bridge's own environment must not win while a request is pointed at.
    monkeypatch.setenv("TUSKER_MCP_MANIFEST", str(tmp_path / "absent.json"))

    (tmp_path / "active.json").write_text(
        json.dumps({"manifest": str(manifest), "call": str(tmp_path / "call.json")}),
        encoding="utf-8",
    )
    assert [tool["name"] for tool in mcp_stdio._load_manifest()] == ["report_value"]

    (tmp_path / "active.json").write_text("{}", encoding="utf-8")
    assert mcp_stdio._load_manifest() == []


def test_mcp_bridge_publishes_into_brokered_call_channel(monkeypatch, tmp_path):
    from tusker_gateway.provider_adapters import mcp_stdio

    call_file = tmp_path / "call.json"
    (tmp_path / "active.json").write_text(
        json.dumps({"manifest": str(tmp_path / "tools.json"), "call": str(call_file)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TUSKER_MCP_BROKER_DIR", str(tmp_path))

    mcp_stdio._publish_call({"name": "report_value"}, {"value": "ok"})

    published = json.loads(call_file.read_text(encoding="utf-8"))
    assert published["name"] == "report_value"
    assert published["arguments"] == {"value": "ok"}


class _WarmKiloServer:
    """Minimal Kilo HTTP server that publishes a brokered tool call on demand."""

    def __init__(self, *, fail_message: bool = False, publish: bool = True) -> None:
        self.fail_message = fail_message
        self.publish = publish
        self.bodies: list[dict] = []
        self.created: list[str] = []
        self.aborted: list[str] = []
        self.deleted: list[str] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/session", self._create)
        app.router.add_post("/session/{sid}/message", self._message)
        app.router.add_post("/session/{sid}/abort", self._abort)
        app.router.add_delete("/session/{sid}", self._delete)
        return app

    async def _create(self, _request):
        session_id = f"ses_warm_{len(self.created)}"
        self.created.append(session_id)
        return web.json_response({"id": session_id})

    async def _message(self, request):
        self.bodies.append(await request.json())
        if self.fail_message:
            return web.json_response({"error": {"code": "kilo_cli_failed"}}, status=502)
        if self.publish:
            pointer = Path(os.environ["TUSKER_MCP_BROKER_DIR"]) / "active.json"
            active = json.loads(pointer.read_text(encoding="utf-8"))
            Path(active["call"]).write_text(json.dumps({
                "id": "call_warm_1", "name": "report_value", "arguments": {"value": "ok"},
            }), encoding="utf-8")
        return web.json_response({
            "info": {"role": "assistant"},
            "parts": [{"type": "text", "text": "done"}],
        })

    async def _abort(self, request):
        self.aborted.append(request.match_info["sid"])
        return web.json_response(True)

    async def _delete(self, request):
        self.deleted.append(request.match_info["sid"])
        return web.json_response(True)


def _warm_tool_env(monkeypatch, tmp_path, server_url: str) -> None:
    monkeypatch.setenv("TUSKER_KILO_CLI_ENABLED", "true")
    monkeypatch.delenv("TUSKER_KILO_WORKER_URL", raising=False)
    monkeypatch.delenv("TUSKER_KILO_WARM_SERVER_URL", raising=False)
    monkeypatch.setenv("TUSKER_KILO_WARM_TOOL_SERVER_URL", server_url)
    monkeypatch.setenv("TUSKER_MCP_BROKER_DIR", str(tmp_path))


@pytest.mark.asyncio
async def test_kilo_warm_tool_path_enables_and_returns_brokered_tool_call(monkeypatch, tmp_path):
    server = _WarmKiloServer()
    client = TestClient(TestServer(server.app()))
    await client.start_server()
    try:
        _warm_tool_env(monkeypatch, tmp_path, str(client.make_url("/")).rstrip("/"))
        result = await KiloCLIAdapter().chat(
            provider="kilo-cli",
            model="kilo-cli/groq/openai/gpt-oss-20b",
            messages=[{"role": "user", "content": "call the tool"}],
            stream=False,
            tools=[_tool()],
            tool_choice=None,
        )
    finally:
        await client.close()

    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"] == {
        "name": "report_value", "arguments": '{"value":"ok"}',
    }
    assert result["model"] == "kilo-cli/groq/openai/gpt-oss-20b"

    body = server.bodies[0]
    # Kilo enables tools by its fully-qualified MCP id, not by our manifest name.
    assert body["tools"] == {"gateway_gateway_tool_0": True}
    assert body["model"] == {"providerID": "groq", "modelID": "openai/gpt-oss-20b"}
    assert "call the tool" in body["parts"][0]["text"]
    assert server.aborted == ["ses_warm_0"]
    assert server.deleted == ["ses_warm_0"]
    # The shared bridge must not keep serving the finished request's tools.
    assert json.loads((tmp_path / "active.json").read_text(encoding="utf-8")) == {}


@pytest.mark.asyncio
async def test_kilo_warm_tool_path_returns_text_when_no_tool_call_is_published(monkeypatch, tmp_path):
    server = _WarmKiloServer(publish=False)
    client = TestClient(TestServer(server.app()))
    await client.start_server()
    try:
        _warm_tool_env(monkeypatch, tmp_path, str(client.make_url("/")).rstrip("/"))
        result = await KiloCLIAdapter().chat(
            provider="kilo-cli", model="kilo-cli/kilo/kilo-auto/free",
            messages=[{"role": "user", "content": "just answer"}],
            stream=False, tools=[_tool()], tool_choice=None,
        )
    finally:
        await client.close()

    assert result["choices"][0]["message"]["content"] == "done"
    assert result["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_kilo_warm_tool_failure_falls_back_to_isolated_cli(monkeypatch, tmp_path):
    server = _WarmKiloServer(fail_message=True)
    client = TestClient(TestServer(server.app()))
    await client.start_server()
    try:
        _warm_tool_env(monkeypatch, tmp_path, str(client.make_url("/")).rstrip("/"))
        with patch("tusker_gateway.provider_adapters.kilo_cli.shutil.which", return_value=None):
            with pytest.raises(ProviderError) as exc:
                await KiloCLIAdapter().chat(
                    provider="kilo-cli", model="kilo-cli/kilo/kilo-auto/free",
                    messages=[{"role": "user", "content": "call the tool"}],
                    stream=False, tools=[_tool()], tool_choice=None,
                )
    finally:
        await client.close()

    # Reaching the isolated path proves the warm failure degraded instead of surfacing.
    assert exc.value.code == "kilo_cli_unavailable"
    assert server.deleted == ["ses_warm_0"]


@pytest.mark.asyncio
async def test_kilo_warm_tool_path_skips_manifests_beyond_authorized_ids(monkeypatch, tmp_path):
    server = _WarmKiloServer()
    client = TestClient(TestServer(server.app()))
    await client.start_server()
    tools = [
        {"type": "function", "function": {
            "name": f"tool_{index}", "parameters": {"type": "object", "properties": {}},
        }}
        for index in range(_WARM_TOOL_INDEX_LIMIT + 1)
    ]
    try:
        _warm_tool_env(monkeypatch, tmp_path, str(client.make_url("/")).rstrip("/"))
        with patch("tusker_gateway.provider_adapters.kilo_cli.shutil.which", return_value=None):
            with pytest.raises(ProviderError):
                await KiloCLIAdapter().chat(
                    provider="kilo-cli", model="kilo-cli/kilo/kilo-auto/free",
                    messages=[{"role": "user", "content": "call the tool"}],
                    stream=False, tools=tools, tool_choice=None,
                )
    finally:
        await client.close()

    # Warm path must not run: the server pre-authorizes a bounded tool id space.
    assert server.created == []