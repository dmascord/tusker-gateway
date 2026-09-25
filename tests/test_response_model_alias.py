"""Pool-route responses echo the requested virtual alias, not the upstream model.

Audit 2026-09-25 finding 2.2: when the gateway selects the concrete backend
(pool / default-code routes), response ``model`` leaked the provider's
internal model id in both the JSON body and every SSE chunk. Explicit
provider-prefixed passthrough keeps upstream identity by design.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.app import create_app
from tusker_gateway.config import PoolConfig, load_config
from tusker_gateway.pools import PoolManager

CONCRETE_MODEL = "qwen-fake-32b-20260101"
ALIASES = ["hermes-code", "hermes-agent", "tusker-gateway::hermes-code"]


class FakeProvider:
    """Returns a concrete model name for both streaming and complete calls."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.bodies.append(body)
        if body.get("stream"):
            resp = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"}
            )
            await resp.prepare(request)
            for delta, finish in (
                ({"role": "assistant", "content": "hello "}, None),
                ({"content": "world"}, "stop"),
            ):
                chunk = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "model": CONCRETE_MODEL,
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": finish}
                    ],
                }
                await resp.write(
                    f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()
                )
            await resp.write(b"data: [DONE]\n\n")
            return resp
        return web.json_response(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "model": CONCRETE_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello world"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                },
            }
        )


def _config(fake_port: int, quality_path: str) -> dict[str, Any]:
    cfg = load_config()
    cfg["providers"]["fake-claude"] = {
        "base_url": f"http://127.0.0.1:{fake_port}",
        "chat_path": "/chat/completions",
        "auth_type": "bearer",
    }
    cfg["pools"]["code"] = PoolConfig(
        name="code",
        models=[{"provider": "fake-claude", "model": CONCRETE_MODEL}],
    )
    cfg["provider_api_keys"]["fake-claude"] = "upstream-key"
    cfg["quality_db_path"] = quality_path
    cfg["api_keys"] = ["test-key"]
    return cfg


def _app(cfg: dict[str, Any]) -> web.Application:
    app = create_app()
    app.on_startup.clear()
    app["config"] = cfg
    app["http_session"] = aiohttp.ClientSession()
    app["pool_manager"] = PoolManager(cfg)
    app["pool_manager"]._quality = app["quality_db"]
    return app


def _sse_data_frames(raw: bytes) -> list[dict[str, Any]]:
    frames = []
    for line in raw.splitlines():
        if line.startswith(b"data: ") and line[6:] != b"[DONE]":
            frames.append(json.loads(line[6:]))
    return frames


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ALIASES)
async def test_pool_route_response_model_echoes_requested_alias(alias):
    fake = FakeProvider()
    fake_app = web.Application()
    fake_app.router.add_post("/chat/completions", fake.handle)
    async with TestServer(fake_app) as fake_server:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _app(_config(fake_server.port, os.path.join(tmpdir, "q.db")))
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": alias, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer test-key"},
                )
                assert resp.status == 200, await resp.text()
                data = await resp.json()
                assert data["model"] == alias
                # The provider still receives the concrete upstream model.
                assert fake.bodies[0]["model"] == CONCRETE_MODEL
            await app["http_session"].close()


@pytest.mark.asyncio
async def test_pool_route_stream_chunks_echo_requested_alias():
    fake = FakeProvider()
    fake_app = web.Application()
    fake_app.router.add_post("/chat/completions", fake.handle)
    async with TestServer(fake_app) as fake_server:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _app(_config(fake_server.port, os.path.join(tmpdir, "q.db")))
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "hermes-code",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                assert resp.status == 200
                assert resp.headers["Content-Type"].startswith("text/event-stream")
                raw = b""
                async for line in resp.content:
                    raw += line
            await app["http_session"].close()

    frames = _sse_data_frames(raw)
    assert frames, raw
    assert {f["model"] for f in frames} == {"hermes-code"}
    assert CONCRETE_MODEL.encode() not in raw


@pytest.mark.asyncio
async def test_explicit_passthrough_keeps_upstream_model():
    """A provider-prefixed model is the client's explicit choice: no rewrite."""
    fake = FakeProvider()
    fake_app = web.Application()
    fake_app.router.add_post("/chat/completions", fake.handle)
    async with TestServer(fake_app) as fake_server:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config(fake_server.port, os.path.join(tmpdir, "q.db"))
            cfg["pools"]["code"] = PoolConfig(name="code", models=[])
            app = _app(cfg)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": f"fake-claude::{CONCRETE_MODEL}",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": "Bearer test-key"},
                )
                assert resp.status == 200, await resp.text()
                data = await resp.json()
                assert data["model"] == CONCRETE_MODEL
            await app["http_session"].close()


@pytest.mark.asyncio
async def test_swarm_marker_route_dispatches_to_swarm_pool():
    """``hermes-gateway/<unknown>`` resolves to the swarm pool, not a 400."""
    from tusker_gateway.routing import resolve_route

    fake = FakeProvider()
    fake_app = web.Application()
    fake_app.router.add_post("/chat/completions", fake.handle)
    async with TestServer(fake_app) as fake_server:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config(fake_server.port, os.path.join(tmpdir, "q.db"))
            cfg["pools"]["swarm"] = PoolConfig(
                name="swarm",
                models=[{"provider": "fake-claude", "model": CONCRETE_MODEL}],
            )
            app = _app(cfg)
            async with TestClient(TestServer(app)) as client:
                marker = "hermes-gateway/some-legacy-role"
                assert resolve_route(marker, {"model": marker}).kind == "swarm"
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": marker, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer test-key"},
                )
                assert resp.status == 200, await resp.text()
                data = await resp.json()
                assert data["choices"][0]["message"]["content"] == "hello world"
                assert fake.bodies[0]["model"] == CONCRETE_MODEL
            await app["http_session"].close()


@pytest.mark.asyncio
async def test_swarm_pool_without_models_returns_no_healthy():
    """An empty swarm pool fails with the pool-specific error, not unsupported_route."""
    fake = FakeProvider()
    fake_app = web.Application()
    fake_app.router.add_post("/chat/completions", fake.handle)
    async with TestServer(fake_app) as fake_server:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config(fake_server.port, os.path.join(tmpdir, "q.db"))
            cfg["pools"]["swarm"] = PoolConfig(name="swarm", models=[])
            app = _app(cfg)
            async with TestClient(TestServer(app)) as client:
                marker = "hermes-reflect/anything"
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": marker, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer test-key"},
                )
                body_text = await resp.text()
                assert "Unsupported model route" not in body_text
            await app["http_session"].close()
