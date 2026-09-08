"""Provider aliases preserve public identity without rewriting generated content."""
from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.config import _provider_registry_from_env
from tusker_gateway.endpoints import models_handler
from tusker_gateway.passthrough import PassthroughClient
from tusker_gateway.quality import QualityDB
from tusker_gateway.routing import resolve_route

ALIAS = "qwen3-coder-30b-a3b-instruct-4bit"
UPSTREAM = "/Users/tusker/models/Qwen3-Coder-30B-A3B-Instruct-4bit"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,separator", [(False, "/"), (True, "::")])
async def test_local_alias_roundtrip(tmp_path, monkeypatch, stream, separator):
    async def upstream(request):
        body = await request.json()
        if body["model"] != UPSTREAM:
            return web.json_response({"error": "unknown model"}, status=404)
        if not body["stream"]:
            return web.json_response({
                "model": UPSTREAM,
                "choices": [{"message": {"role": "assistant", "content": UPSTREAM}, "finish_reason": "stop"}],
            })
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        frame = b"data: " + json.dumps({
            "model": UPSTREAM,
            "choices": [{"index": 0, "delta": {"content": UPSTREAM}, "finish_reason": "stop"}],
        }, separators=(",", ":")).encode() + b"\r\n\r\n"
        split = frame.index(b"/Users/") + 4
        await response.write(frame[:split])
        await asyncio.sleep(0.01)
        await response.write(frame[split:] + b"data: [DONE]\r\n\r\n")
        return response

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream)
    async with TestServer(upstream_app) as server:
        monkeypatch.setenv("PROVIDER_REGISTRY_JSON", json.dumps({"mlx-mac": {
            "kind": "local", "base_url": str(server.make_url("")).rstrip("/"),
            "model_aliases": {ALIAS: UPSTREAM},
        }}))
        config = {
            "providers": _provider_registry_from_env(), "api_keys": ["test"],
            "quality_db_path": str(tmp_path / "quality.db"),
        }
        app = web.Application()
        app["config"] = {**config, "model_name": "gateway"}
        app.router.add_get("/v1/models", models_handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/v1/models")
            ids = {row["id"] for row in (await response.json())["data"]}
            assert f"mlx-mac/{ALIAS}" in ids
            assert not any("/Users/" in model for model in ids)
        async with aiohttp.ClientSession() as http:
            passthrough = PassthroughClient(config, QualityDB(config["quality_db_path"]), http)
            route = resolve_route(f"mlx-mac{separator}{ALIAS}", {})
            assert route.kind == "passthrough"
            result = await passthrough.chat(route.provider, route.model, [{"role": "user", "content": "hello"}], stream=stream, rtk_compress=False)
            if stream:
                output = b"".join([chunk async for chunk in result]).decode()
                frames = [json.loads(line[6:]) for line in output.splitlines() if line.startswith("data: ") and line[6:] != "[DONE]"]
                assert frames[0]["model"] == ALIAS
                assert frames[0]["choices"][0]["delta"]["content"] == UPSTREAM
                assert "data: [DONE]" in output
            else:
                assert result["model"] == ALIAS
                assert result["choices"][0]["message"]["content"] == UPSTREAM
