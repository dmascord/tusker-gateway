from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from tusker_gateway import kilo_worker
from tusker_gateway.provider_adapters.kilo_cli import KiloCLIAdapter


@pytest.mark.asyncio
async def test_worker_health_is_available():
    client = TestClient(TestServer(kilo_worker.create_app()))
    await client.start_server()
    try:
        response = await client.get("/healthz")
        assert response.status == 200
        assert (await response.json())["worker"] == "kilo"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_worker_rejects_models_outside_qualified_allowlist(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(kilo_worker.KiloCLIAdapter, "chat", run)
    client = TestClient(TestServer(kilo_worker.create_app()))
    await client.start_server()
    try:
        response = await client.post("/v1/chat/completions", json={
            "model": "groq/openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": "test"}],
        })
        assert response.status == 400
        assert (await response.json())["error"]["code"] == "unsupported_model"
        run.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", sorted(kilo_worker._ALLOWED_MODELS))
async def test_worker_forwards_allowed_tool_requests_without_execution(monkeypatch, model):
    completion = {"choices": [{"message": {"role": "assistant", "tool_calls": [
        {"id": "call_test", "type": "function", "function": {
            "name": "report_value", "arguments": '{"value":"ok"}',
        }},
    ]}, "finish_reason": "tool_calls"}]}
    run = AsyncMock(return_value=completion)
    monkeypatch.setattr(kilo_worker.KiloCLIAdapter, "chat", run)
    client = TestClient(TestServer(kilo_worker.create_app()))
    await client.start_server()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "call report_value"}],
        "tools": [{"type": "function", "function": {"name": "report_value"}}],
        "tool_choice": "required",
    }
    try:
        response = await client.post("/v1/chat/completions", json=body)
        assert response.status == 200
        result = await response.json()
        assert result["choices"][0]["finish_reason"] == "tool_calls"
        run.assert_awaited_once_with(
            provider="kilo-cli", model=model, messages=body["messages"],
            stream=False, tools=body["tools"], tool_choice="required",
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gateway_kilo_adapter_proxies_to_worker_and_restores_public_model(monkeypatch):
    seen = {}

    async def respond(request):
        seen.update(await request.json())
        return web.json_response({"id": "chatcmpl-worker", "choices": [{
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", respond)
    client = TestClient(TestServer(app))
    await client.start_server()
    monkeypatch.setenv("TUSKER_KILO_CLI_ENABLED", "true")
    monkeypatch.setenv("TUSKER_KILO_WORKER_URL", str(client.make_url("/")))
    messages = [{"role": "user", "content": "hello"}]
    try:
        result = await KiloCLIAdapter().chat(
            provider="kilo-cli",
            model="kilo-cli/groq/openai/gpt-oss-20b",
            messages=messages,
            stream=False,
            tools=None,
            tool_choice=None,
        )
        assert result["model"] == "kilo-cli/groq/openai/gpt-oss-20b"
        assert seen["model"] == "groq/openai/gpt-oss-20b"
        assert seen["messages"] == messages
    finally:
        await client.close()
