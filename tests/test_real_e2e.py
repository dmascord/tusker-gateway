"""Real end-to-end tests against a live provider.

Requires OPENROUTER_API_KEY in the environment.
Uses openai/gpt-4o-mini as the low-cost real target model.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.app import create_app
from tusker_gateway.config import load_config
from tusker_gateway.passthrough import PROVIDER_ENDPOINTS

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not OPENROUTER_KEY,
    reason="OPENROUTER_API_KEY not set; skipping real-provider tests",
)

# Shared key for both gateways in chained tests
SHARED_KEY = "real-test-key"


def _test_app(config: dict[str, Any]) -> web.Application:
    app = create_app()
    app.on_startup.clear()  # prevent default handler from overwriting config
    app["config"] = config
    app["http_session"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120),
    )
    return app


def _real_config(quality_path: str, *, api_keys: list[str] | None = None, openai_codex_port: int | None = None) -> dict[str, Any]:
    cfg = load_config()
    cfg["api_keys"] = api_keys or [SHARED_KEY]
    cfg["quality_db_path"] = quality_path
    # Ensure openai-codex (patched to OpenRouter) can find its bearer key.
    # OPENROUTER_API_KEY is loaded into provider_api_keys["openrouter"] by load_config;
    # the codex patch reuses it via provider_api_keys["openai-codex"].
    if OPENROUTER_KEY:
        cfg["provider_api_keys"]["openai-codex"] = OPENROUTER_KEY
    if openai_codex_port is not None:
        cfg.setdefault("providers", {})["openai-codex"] = {
            "base_url": f"http://127.0.0.1:{openai_codex_port}",
            "chat_path": "/chat/completions",
            "auth_type": "bearer",
        }
    return cfg


@pytest.mark.asyncio
async def test_real_single_gateway_chat():
    """Single tusker-gateway → fake OpenRouter-compatible capture server."""
    with tempfile.TemporaryDirectory() as tmpdir:
        captured_requests = []

        async def capture_handler(request: web.Request) -> web.Response:
            body = await request.json()
            captured_requests.append(body)
            return web.json_response({
                "id": "test",
                "object": "chat.completion",
                "model": body.get("model", "unknown"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "E2E_TEST_OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        capture_app = web.Application()
        capture_app.router.add_post("/chat/completions", capture_handler)
        capture_server = TestServer(capture_app)
        capture_client = TestClient(capture_server)
        await capture_client.start_server()

        try:
            cfg = _real_config(os.path.join(tmpdir, "quality.db"), openai_codex_port=capture_server.port)
            app = _test_app(cfg)
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()

            payload = {
                "model": "hermes-privacy",
                "messages": [{"role": "user", "content": "Say exactly: E2E_TEST_OK"}],
                "stream": False,
            }
            resp = await client.post(
                "/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {SHARED_KEY}"},
            )
            assert resp.status == 200, f"Status {resp.status}: {await resp.text()}"
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            assert "E2E_TEST_OK" in content, f"Unexpected content: {content}"

            assert len(captured_requests) == 1
            model_sent = captured_requests[0].get("model", "")
            assert model_sent != "hermes-privacy", f"Virtual alias leaked to provider: {model_sent}"
            print(f"\n✅ Single gateway response: {content!r} (model sent: {model_sent!r})")

            await client.close()
        finally:
            await capture_client.close()

@pytest.mark.asyncio
async def test_real_single_gateway_stream():
    """Single tusker-gateway → fake OpenRouter-compatible capture server, streaming."""
    with tempfile.TemporaryDirectory() as tmpdir:
        async def capture_handler(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            stream = bool(body and body.get("stream", False))
            if stream:
                resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
                await resp.prepare(request)
                chunk1 = {"id": "chatcmpl-fake1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "STREAM_OK"}, "finish_reason": None}]}
                chunk2 = {"id": "chatcmpl-fake2", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}]}
                await resp.write(f"data: {chunk1}\n\n".encode())
                await resp.write(f"data: {chunk2}\n\n".encode())
                await resp.write(b"data: [DONE]\n\n")
                return resp
            return web.json_response({
                "id": "test",
                "object": "chat.completion",
                "model": body.get("model", "unknown") if body else "unknown",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "STREAM_OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        capture_app = web.Application()
        capture_app.router.add_post("/chat/completions", capture_handler)
        capture_server = TestServer(capture_app)
        capture_client = TestClient(capture_server)
        await capture_client.start_server()

        try:
            cfg = _real_config(os.path.join(tmpdir, "quality.db"), openai_codex_port=capture_server.port)
            app = _test_app(cfg)
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()

            payload = {
                "model": "hermes-privacy",
                "messages": [{"role": "user", "content": "Say exactly: STREAM_OK"}],
                "stream": True,
            }
            resp = await client.post(
                "/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {SHARED_KEY}"},
            )
            assert resp.status == 200, f"Status {resp.status}: {await resp.text()}"
            assert resp.headers["Content-Type"].startswith("text/event-stream")

            chunks = []
            async for line in resp.content:
                chunks.append(line)
            body = b"".join(chunks)
            assert b"[DONE]" in body, "Missing [DONE] sentinel"
            assert b"STREAM_OK" in body, f"Missing expected text in stream: {body[:500]}"
            print(f"\n✅ Streaming response ({len(body)} bytes): received [DONE]")

            await client.close()
        finally:
            await capture_client.close()

@pytest.mark.asyncio
async def test_real_chained_gateways():
    """gateway-1 → gateway-2 → fake OpenRouter-compatible capture server."""
    with tempfile.TemporaryDirectory() as tmpdir:
        captured_requests = []

        async def capture_handler(request: web.Request) -> web.Response:
            body = await request.json()
            captured_requests.append(body)
            return web.json_response({
                "id": "test",
                "object": "chat.completion",
                "model": body.get("model", "unknown"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "CHAIN_OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        capture_app = web.Application()
        capture_app.router.add_post("/chat/completions", capture_handler)
        capture_server = TestServer(capture_app)
        capture_client = TestClient(capture_server)
        await capture_client.start_server()

        try:
            # gateway-2: routes to fake OpenRouter on capture server
            cfg2 = _real_config(os.path.join(tmpdir, "g2.db"), openai_codex_port=capture_server.port)
            g2_app = _test_app(cfg2)
            g2_server = TestServer(g2_app)
            g2_client = TestClient(g2_server)
            await g2_client.start_server()

            # gateway-1: routes to gateway-2
            cfg1 = _real_config(os.path.join(tmpdir, "g1.db"), openai_codex_port=capture_server.port)
            cfg1["upstream_gateway_url"] = f"http://127.0.0.1:{g2_server.port}"
            g1_app = _test_app(cfg1)
            g1_server = TestServer(g1_app)
            g1_client = TestClient(g1_server)
            await g1_client.start_server()

            payload = {
                "model": "hermes-privacy",
                "messages": [{"role": "user", "content": "Say exactly: CHAIN_OK"}],
                "stream": False,
            }
            resp = await g1_client.post(
                "/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {SHARED_KEY}"},
            )
            assert resp.status == 200, f"Status {resp.status}: {await resp.text()}"
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            assert "CHAIN_OK" in content, f"Unexpected content: {content}"

            assert len(captured_requests) == 1
            model_sent = captured_requests[0].get("model", "")
            assert model_sent != "hermes-privacy", f"Virtual alias leaked to provider: {model_sent}"
            print(f"\n✅ Chained gateway response: {content!r} (model sent: {model_sent!r})")

            await g1_client.close()
            await g2_client.close()
        finally:
            await capture_client.close()


@pytest.mark.asyncio
async def test_real_virtual_alias_not_persisted():
    """The virtual alias must be resolved internally, never sent to the provider."""
    with tempfile.TemporaryDirectory() as tmpdir:
        captured_requests = []

        async def capture_handler(request: web.Request) -> web.Response:
            body = await request.json()
            captured_requests.append(body)
            return web.json_response({
                "id": "test",
                "object": "chat.completion",
                "model": body.get("model", "unknown"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        capture_app = web.Application()
        capture_app.router.add_post("/chat/completions", capture_handler)
        capture_server = TestServer(capture_app)
        capture_client = TestClient(capture_server)
        await capture_client.start_server()

        try:
            # Single gateway, route via fake OpenRouter-compatible server
            cfg = _real_config(os.path.join(tmpdir, "quality.db"), openai_codex_port=capture_server.port)
            app = _test_app(cfg)
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()

            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "hermes-privacy", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {SHARED_KEY}"},
            )
            assert resp.status == 200, f"Status {resp.status}: {await resp.text()}"

            assert len(captured_requests) == 1
            model_sent = captured_requests[0].get("model", "")
            assert model_sent != "hermes-privacy", f"Virtual alias leaked to provider: {model_sent}"
            print(f"\n✅ Virtual alias guard: sent {model_sent!r} (not 'hermes-privacy')")

            await client.close()
        finally:
            await capture_client.close()

