from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from tusker_gateway import kilo_worker
from tusker_gateway.errors import BadRequestError, ProviderError
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
async def test_worker_relays_incremental_cli_sse(monkeypatch):
    async def chunks():
        yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
        yield b'data: [DONE]\n\n'

    run = AsyncMock(return_value=chunks())
    monkeypatch.setattr(kilo_worker.KiloCLIAdapter, "chat", run)
    client = TestClient(TestServer(kilo_worker.create_app()))
    await client.start_server()
    try:
        response = await client.post("/v1/chat/completions", json={
            "model": "kilo/kilo-auto/free",
            "public_model": "kilo-cli/kilo/kilo-auto/free",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        })
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")
        assert b'"content":"first"' in await response.read()
        run.assert_awaited_once()
        assert run.await_args.kwargs["model"] == "kilo-cli/kilo/kilo-auto/free"
        assert run.await_args.kwargs["stream"] is True
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



async def _worker_returning(payload, status):
    """Point the adapter at a stub worker that always returns one response."""

    async def respond(_request):
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", respond)
    worker = TestClient(TestServer(app))
    await worker.start_server()
    return worker


@pytest.mark.asyncio
async def test_gateway_kilo_adapter_preserves_worker_client_rejection(monkeypatch):
    """A worker 4xx describes the request, so the client must see its own error."""
    worker = await _worker_returning(
        {
            "error": {
                "code": "unsupported_message_content",
                "message": "kilo-cli currently accepts text-only message content",
            }
        },
        400,
    )
    monkeypatch.setenv("TUSKER_KILO_CLI_ENABLED", "true")
    monkeypatch.setenv("TUSKER_KILO_WORKER_URL", str(worker.make_url("/")))
    try:
        with pytest.raises(BadRequestError) as exc:
            await KiloCLIAdapter().chat(
                provider="kilo-cli",
                model="kilo-cli/kilo/kilo-auto/free",
                messages=[{"role": "user", "content": "hello"}],
                stream=False,
                tools=None,
                tool_choice=None,
            )
    finally:
        await worker.close()
    assert exc.value.status == 400
    assert exc.value.code == "unsupported_message_content"
    assert "text-only message content" in exc.value.message


@pytest.mark.asyncio
async def test_gateway_kilo_adapter_preserves_worker_request_status(monkeypatch):
    """Request-shaped statuses other than 400 reach the client unchanged."""
    worker = await _worker_returning(
        {"error": {"code": "unsupported_message_content", "message": "no images"}},
        422,
    )
    monkeypatch.setenv("TUSKER_KILO_CLI_ENABLED", "true")
    monkeypatch.setenv("TUSKER_KILO_WORKER_URL", str(worker.make_url("/")))
    try:
        with pytest.raises(BadRequestError) as exc:
            await KiloCLIAdapter().chat(
                provider="kilo-cli",
                model="kilo-cli/kilo/kilo-auto/free",
                messages=[{"role": "user", "content": "hello"}],
                stream=False,
                tools=None,
                tool_choice=None,
            )
    finally:
        await worker.close()
    assert exc.value.status == 422


@pytest.mark.asyncio
async def test_gateway_kilo_adapter_reports_worker_outage_as_bad_gateway(monkeypatch):
    """A worker-side failure stays a 502 provider error, not a client error."""
    worker = await _worker_returning(
        {"error": {"code": "kilo_worker_failed", "message": "Kilo worker request failed"}},
        500,
    )
    monkeypatch.setenv("TUSKER_KILO_CLI_ENABLED", "true")
    monkeypatch.setenv("TUSKER_KILO_WORKER_URL", str(worker.make_url("/")))
    try:
        with pytest.raises(ProviderError) as exc:
            await KiloCLIAdapter().chat(
                provider="kilo-cli",
                model="kilo-cli/kilo/kilo-auto/free",
                messages=[{"role": "user", "content": "hello"}],
                stream=False,
                tools=None,
                tool_choice=None,
            )
    finally:
        await worker.close()
    assert not isinstance(exc.value, BadRequestError)
    assert exc.value.status == 502
    assert exc.value.code == "kilo_worker_failed"


@pytest.mark.asyncio
async def test_gateway_kilo_adapter_stream_preserves_worker_client_rejection(monkeypatch):
    """The streaming path reports the same client error as the buffered path."""
    worker = await _worker_returning(
        {"error": {"code": "unsupported_message_content", "message": "text only"}},
        400,
    )
    monkeypatch.setenv("TUSKER_KILO_CLI_ENABLED", "true")
    monkeypatch.setenv("TUSKER_KILO_WORKER_URL", str(worker.make_url("/")))
    try:
        result = await KiloCLIAdapter().chat(
            provider="kilo-cli",
            model="kilo-cli/kilo/kilo-auto/free",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
            tools=None,
            tool_choice=None,
        )
        with pytest.raises(BadRequestError) as exc:
            async for _chunk in result:
                pass
    finally:
        await worker.close()
    assert exc.value.code == "unsupported_message_content"


@pytest.mark.asyncio
async def test_gateway_kilo_adapter_defaults_non_string_worker_error_fields(monkeypatch):
    """A malformed worker error body must not raise a construction error."""
    worker = await _worker_returning(
        {"error": {"code": 42, "message": None}},
        400,
    )
    monkeypatch.setenv("TUSKER_KILO_CLI_ENABLED", "true")
    monkeypatch.setenv("TUSKER_KILO_WORKER_URL", str(worker.make_url("/")))
    try:
        with pytest.raises(BadRequestError) as exc:
            await KiloCLIAdapter().chat(
                provider="kilo-cli",
                model="kilo-cli/kilo/kilo-auto/free",
                messages=[{"role": "user", "content": "hello"}],
                stream=False,
                tools=None,
                tool_choice=None,
            )
    finally:
        await worker.close()
    assert exc.value.code == "kilo_worker_failed"
    assert exc.value.message == "Kilo worker request failed"
