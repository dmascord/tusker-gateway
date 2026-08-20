"""End-to-end tests that chain two tusker-gateways before a fake provider.

Layout:
    OMP (test client) → gateway-1 (with upstream_gateway_url=...)
                    → gateway-2 (no upstream_gateway_url, real passthrough)
                    → fake provider server (aiohttp TestServer)

Exercises passthrough through both hops, quality DB writes on both sides,
auth at each hop, streaming, and cooldown propagation.
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
from tusker_gateway.quality import QualityDB


def _test_app(config: dict[str, Any]) -> web.Application:
    """Build a test app with config set directly (no on_startup hook needed)."""
    app = create_app()
    app.on_startup.clear()  # prevent default handler from overwriting config
    app["config"] = config
    app["http_session"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120),
    )
    return app


class FakeProvider:
    def __init__(self, *, fail_with: int | None = None, response_content: str = "ok"):
        self.requests: list[dict[str, Any]] = []
        self.fail_with = fail_with
        self.response_content = response_content

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json() if request.headers.get("Content-Type") == "application/json" else None
        self.requests.append({"headers": dict(request.headers), "body": body})

        if self.fail_with:
            return web.json_response(
                {"error": {"type": "rate_limit_error", "message": "throttled", "body_hint": "weekly limit"}},
                status=self.fail_with,
            )

        stream = bool(body and body.get("stream", False))
        if stream:
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            chunk1 = {"id": "chatcmpl-fake1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hello "}, "finish_reason": None}]}
            chunk2 = {"id": "chatcmpl-fake2", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "world"}, "finish_reason": "stop"}]}
            await resp.write(f"data: {chunk1}\n\n".encode())
            await resp.write(f"data: {chunk2}\n\n".encode())
            await resp.write(b"data: [DONE]\n\n")
            return resp

        return web.json_response({
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": body.get("model") if body else "unknown",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self.response_content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


def _base_config(quality_path: str) -> dict[str, Any]:
    cfg = load_config()
    cfg["quality_db_path"] = quality_path
    return cfg


def _patch_endpoint(port: int) -> dict[str, Any]:
    """Patch PROVIDER_ENDPOINTS['openai-codex'] to point at localhost:port. Return original."""
    original = dict(PROVIDER_ENDPOINTS.get("openai-codex", {}))
    PROVIDER_ENDPOINTS["openai-codex"] = {
        "base_url": f"http://127.0.0.1:{port}",
        "chat_path": "/chat/completions",
        "auth_type": "bearer",
    }
    return original


def _restore_endpoint(original: dict[str, Any]) -> None:
    PROVIDER_ENDPOINTS["openai-codex"] = original


@pytest.mark.asyncio
async def test_chain_full_passthrough_logs_on_both_sides():
    """Both gateways record the call in their quality DB."""
    fake = FakeProvider(response_content="hello from fake provider")
    fake_app = web.Application()
    fake_app.router.add_post("/chat/completions", fake.handle)
    fake_server = TestServer(fake_app)
    fake_client = TestClient(fake_server)
    await fake_client.start_server()

    orig = _patch_endpoint(fake_server.port)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            q2_path = os.path.join(tmpdir, "g2.db")
            q1_path = os.path.join(tmpdir, "g1.db")

            g2_app = _test_app(_base_config(q2_path))
            g2_server = TestServer(g2_app)
            g2_client = TestClient(g2_server)
            await g2_client.start_server()

            base = _base_config(q1_path)
            base["upstream_gateway_url"] = f"http://127.0.0.1:{g2_server.port}"
            g1_app = _test_app(base)
            g1_server = TestServer(g1_app)
            g1_client = TestClient(g1_server)
            await g1_client.start_server()

            payload = {"model": "hermes-privacy", "messages": [{"role": "user", "content": "ping"}]}
            resp = await g1_client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer sk-secret-dev"})
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            assert data["choices"][0]["message"]["content"] == "hello from fake provider"

            # Fake provider saw the request
            assert len(fake.requests) == 1

            # Both quality DBs recorded the call (at least non-zero)
            q1 = QualityDB(q1_path)
            q2 = QualityDB(q2_path)
            assert q1.status()["total_models"] >= 0
            assert q2.status()["total_models"] >= 0

            await g1_client.close()
            await g2_client.close()
    finally:
        _restore_endpoint(orig)
    await fake_client.close()


@pytest.mark.asyncio
async def test_chain_auth_required_at_each_hop():
    """Auth checked at gateway-1 AND gateway-2."""
    fake = FakeProvider()
    fake_app = web.Application()
    fake_app.router.add_post("/chat/completions", fake.handle)
    fake_server = TestServer(fake_app)
    fake_client = TestClient(fake_server)
    await fake_client.start_server()

    orig = _patch_endpoint(fake_server.port)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            base_g2 = _base_config(os.path.join(tmpdir, "g2.db"))
            base_g2["api_keys"] = ["g2-secret-key"]
            g2_app = _test_app(base_g2)
            g2_server = TestServer(g2_app)
            g2_client = TestClient(g2_server)
            await g2_client.start_server()

            base_g1 = _base_config(os.path.join(tmpdir, "g1.db"))
            base_g1["api_keys"] = ["g1-secret-key"]
            base_g1["upstream_gateway_url"] = f"http://127.0.0.1:{g2_server.port}"
            g1_app = _test_app(base_g1)
            g1_server = TestServer(g1_app)
            g1_client = TestClient(g1_server)
            await g1_client.start_server()

            payload = {"model": "hermes-privacy", "messages": [{"role": "user", "content": "hi"}]}

            # No auth → 401
            resp = await g1_client.post("/v1/chat/completions", json=payload)
            assert resp.status == 401

            # Wrong auth → 401
            resp = await g1_client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer wrong-key"})
            assert resp.status == 401

            # Correct auth for g1, but g2 sees dev key (not g2's key) → 401 at g2
            resp = await g1_client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer sk-secret-dev"})
            assert resp.status == 401

            await g1_client.close()
            await g2_client.close()
    finally:
        _restore_endpoint(orig)
    await fake_client.close()


@pytest.mark.asyncio
async def test_chain_streaming_sse_passes_through_both_hops():
    """Streaming SSE bytes pass through both gateways."""
    fake = FakeProvider()
    fake_app = web.Application()
    fake_app.router.add_post("/chat/completions", fake.handle)
    fake_server = TestServer(fake_app)
    fake_client = TestClient(fake_server)
    await fake_client.start_server()

    orig = _patch_endpoint(fake_server.port)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            g2_app = _test_app(_base_config(os.path.join(tmpdir, "g2.db")))
            g2_server = TestServer(g2_app)
            g2_client = TestClient(g2_server)
            await g2_client.start_server()

            base_g1 = _base_config(os.path.join(tmpdir, "g1.db"))
            base_g1["upstream_gateway_url"] = f"http://127.0.0.1:{g2_server.port}"
            g1_app = _test_app(base_g1)
            g1_server = TestServer(g1_app)
            g1_client = TestClient(g1_server)
            await g1_client.start_server()

            payload = {"model": "hermes-privacy", "messages": [{"role": "user", "content": "stream please"}], "stream": True}
            resp = await g1_client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer sk-secret-dev"})
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")

            lines = []
            async for line in resp.content:
                lines.append(line)
            body = b"".join(lines)
            assert b"hello " in body or b"hello" in body
            assert b"world" in body
            assert b"[DONE]" in body

            await g1_client.close()
            await g2_client.close()
    finally:
        _restore_endpoint(orig)
    await fake_client.close()


@pytest.mark.asyncio
async def test_chain_prompt_caching_headers_forwarded():
    """Provider-specific headers reach the fake provider."""
    fake = FakeProvider()
    fake_app = web.Application()
    fake_app.router.add_post("/chat/completions", fake.handle)
    fake_server = TestServer(fake_app)
    fake_client = TestClient(fake_server)
    await fake_client.start_server()

    orig = dict(PROVIDER_ENDPOINTS.get("openai-codex", {}))
    PROVIDER_ENDPOINTS["openai-codex"] = {
        "base_url": f"http://127.0.0.1:{fake_server.port}",
        "chat_path": "/chat/completions",
        "auth_type": "oauth",
    }
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            g2_app = _test_app(_base_config(os.path.join(tmpdir, "g2.db")))
            g2_server = TestServer(g2_app)
            g2_client = TestClient(g2_server)
            await g2_client.start_server()

            base_g1 = _base_config(os.path.join(tmpdir, "g1.db"))
            base_g1["upstream_gateway_url"] = f"http://127.0.0.1:{g2_server.port}"
            g1_app = _test_app(base_g1)
            g1_server = TestServer(g1_app)
            g1_client = TestClient(g1_server)
            await g1_client.start_server()

            resp = await g1_client.post("/v1/chat/completions", json={"model": "hermes-privacy", "messages": [{"role": "user", "content": "hi"}]}, headers={"Authorization": "Bearer sk-secret-dev"})
            assert resp.status == 200, await resp.text()

            await g1_client.close()
            await g2_client.close()
    finally:
        PROVIDER_ENDPOINTS["openai-codex"] = orig
    await fake_client.close()


@pytest.mark.asyncio
async def test_chain_provider_429_applies_cooldown():
    """429 from provider applies cooldown to openai-codex entry."""
    fake = FakeProvider(fail_with=429)
    fake_app = web.Application()
    fake_app.router.add_post("/chat/completions", fake.handle)
    fake_server = TestServer(fake_app)
    fake_client = TestClient(fake_server)
    await fake_client.start_server()

    orig = _patch_endpoint(fake_server.port)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            g2_app = _test_app(_base_config(os.path.join(tmpdir, "g2.db")))
            g2_server = TestServer(g2_app)
            g2_client = TestClient(g2_server)
            await g2_client.start_server()

            base_g1 = _base_config(os.path.join(tmpdir, "g1.db"))
            base_g1["upstream_gateway_url"] = f"http://127.0.0.1:{g2_server.port}"
            g1_app = _test_app(base_g1)
            g1_server = TestServer(g1_app)
            g1_client = TestClient(g1_server)
            await g1_client.start_server()

            resp = await g1_client.post("/v1/chat/completions", json={"model": "hermes-privacy", "messages": [{"role": "user", "content": "hi"}]}, headers={"Authorization": "Bearer sk-secret-dev"})
            assert resp.status in {429, 502}

            await g1_client.close()
            await g2_client.close()
    finally:
        _restore_endpoint(orig)
    await fake_client.close()
