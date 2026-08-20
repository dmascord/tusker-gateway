"""Integration tests for cache + budget + metrics wired into chat handler."""
from __future__ import annotations

import json
import os
import tempfile

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.app import create_app
from tusker_gateway.budget import BudgetTracker, BudgetConfig, BudgetCaps, _key_fingerprint
from tusker_gateway.cache import ResponseCache, CacheConfig
from tusker_gateway.metrics import MetricsRegistry


@pytest_asyncio.fixture
async def client():
    # The default create_app() instantiates cache/budget/metrics with
    # env-driven defaults (all disabled). We override them via env vars
    # so the handler actually uses them.
    tmp = tempfile.mkdtemp()
    os.environ["TUSKER_CACHE_ENABLED"] = "true"
    os.environ["TUSKER_CACHE_PATH"] = os.path.join(tmp, "cache.db")
    os.environ["TUSKER_CACHE_TTL_SECS"] = "60"
    os.environ["TUSKER_BUDGETS_ENABLED"] = "true"
    os.environ["TUSKER_BUDGETS_PATH"] = os.path.join(tmp, "budget.db")
    api_key = "sk-test-int"
    fp = _key_fingerprint(api_key)
    os.environ["TUSKER_BUDGETS_JSON"] = json.dumps({fp: {"daily_tokens": 1000}})

    app = create_app()
    app.on_startup.clear()
    app["http_session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client, api_key
    await client.close()


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_prometheus_text(client):
    cl, api_key = client
    resp = await cl.get("/metrics")
    assert resp.status == 200
    body = await resp.text()
    assert "# HELP tusker_requests_total" in body
    assert "# TYPE tusker_request_duration_seconds histogram" in body


@pytest.mark.asyncio
async def test_budget_blocks_after_threshold(client):
    cl, api_key = client
    # Need a config where the test api_key is accepted.
    app = cl.server.app
    app["config"]["api_keys"] = [api_key]
    # The handler pre-flight estimates tokens from message chars. A 10k char
    # message is ~2500 tokens, well over the 1000 daily cap.
    big_msg = "x" * 10_000
    payload = {
        "model": "hermes-code",
        "messages": [{"role": "user", "content": big_msg}],
    }
    # This won't actually call a provider because the budget check rejects first.
    resp = await cl.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status == 429
    body = await resp.json()
    assert body["error"]["code"] == "budget_exceeded"
    assert "X-Tusker-Budget-Reason" in resp.headers
    # /metrics now records a budget block.
    metrics_resp = await cl.get("/metrics")
    body = await metrics_resp.text()
    assert "tusker_budget_blocks_total" in body


@pytest.mark.asyncio
async def test_metrics_requires_token_when_configured(client):
    """If TUSKER_METRICS_TOKEN is set, /metrics requires the token header."""
    import importlib
    cl, _ = client
    app = cl.server.app
    # Set the token on the live app via the closure used by create_app.
    # Simpler: re-instantiate with token set.
    prev = os.environ.get("TUSKER_METRICS_TOKEN")
    os.environ["TUSKER_METRICS_TOKEN"] = "secret-token"
    # Re-create app to pick up the env var.
    new_app = create_app()
    new_app["http_session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    new_server = TestServer(new_app)
    new_client = TestClient(new_server)
    await new_client.start_server()
    try:
        # No token -> 401
        resp = await new_client.get("/metrics")
        assert resp.status == 401
        # Wrong token -> 401
        resp = await new_client.get("/metrics", headers={"X-Tusker-Metrics-Token": "wrong"})
        assert resp.status == 401
        # Right token -> 200
        resp = await new_client.get("/metrics", headers={"X-Tusker-Metrics-Token": "secret-token"})
        assert resp.status == 200
    finally:
        await new_client.close()
        if prev is None:
            os.environ.pop("TUSKER_METRICS_TOKEN", None)
        else:
            os.environ["TUSKER_METRICS_TOKEN"] = prev
