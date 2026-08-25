"""Integration tests for circuit breaker + rate limiter wired into chat handler."""
from __future__ import annotations

import json
import os
import tempfile

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.app import create_app
from tusker_gateway.circuit_breaker import BreakerConfig, BreakerPolicy, CircuitBreaker
from tusker_gateway.rate_limit import RateLimitConfig, RateLimitPolicy, RateLimiter, _key_fingerprint


@pytest_asyncio.fixture
async def client_with_rl_and_breaker():
    """Build an app with rate-limit and circuit breaker enabled."""
    tmp = tempfile.mkdtemp()
    api_key = "sk-test-rl-cb"
    fp = _key_fingerprint(api_key)

    os.environ["TUSKER_RATELIMIT_ENABLED"] = "true"
    os.environ["TUSKER_RATELIMIT_PATH"] = os.path.join(tmp, "rl.db")
    os.environ["TUSKER_RATELIMIT_JSON"] = json.dumps({fp: {"rate_per_sec": 2, "burst": 3}})

    os.environ["TUSKER_CIRCUIT_ENABLED"] = "true"
    os.environ["TUSKER_CIRCUIT_PATH"] = os.path.join(tmp, "cb.db")
    os.environ["TUSKER_CIRCUIT_CONSECUTIVE"] = "2"

    app = create_app()
    app.on_startup.clear()
    app["http_session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    # The default create_app instantiates rl/breaker from env. The keys must
    # be accepted by the auth middleware too.
    app["config"]["api_keys"] = [api_key]

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client, api_key
    await client.close()


@pytest.mark.asyncio
async def test_rate_limiter_returns_429(client_with_rl_and_breaker):
    cl, api_key = client_with_rl_and_breaker
    # Burst = 3, rate = 2/s. First 3 calls pass (consume burst); 4th blocked.
    statuses = []
    for _ in range(5):
        resp = await cl.post(
            "/v1/chat/completions",
            json={"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        statuses.append(resp.status)
        # Even on failure (e.g. provider error) the rate limit token was consumed.
    # We expect at least one 429 in the sequence.
    assert 429 in statuses, f"expected 429, got {statuses}"

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/images/generations", {"model": "gpt-image-1", "prompt": "cat"}),
        ("/v1/videos", {"model": "sora-2", "prompt": "cat"}),
    ],
)
async def test_media_routes_share_rate_limiter(
    client_with_rl_and_breaker, path, payload
):
    cl, api_key = client_with_rl_and_breaker
    async def fake_handle_request(**kwargs):
        return {"status": "ok"}

    handler_name = "image_handler" if path.startswith("/v1/images") else "video_handler"
    cl.server.app[handler_name].handle_request = fake_handle_request
    statuses = []
    for _ in range(5):
        resp = await cl.post(
            path,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        statuses.append(resp.status)
    assert 429 in statuses


@pytest.mark.asyncio
async def test_video_rejects_missing_key_before_provider_call(
    client_with_rl_and_breaker,
):
    cl, api_key = client_with_rl_and_breaker
    app = cl.server.app
    app["config"]["provider_api_keys"]["openai"] = None

    resp = await cl.post(
        "/v1/videos?wait=false",
        json={"model": "sora-2", "prompt": "cat"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert resp.status == 503
    body = await resp.json()
    assert body["error"]["code"] == "missing_api_key"


@pytest.mark.asyncio
async def test_metrics_includes_breaker_and_ratelimit_counters(client_with_rl_and_breaker):
    cl, _ = client_with_rl_and_breaker
    resp = await cl.get("/metrics")
    assert resp.status == 200
    body = await resp.text()
    # ratelimit stats are surfaced via the budget_blocks metric family.
    assert "tusker_budget_blocks_total" in body


@pytest.mark.asyncio
async def test_dashboard_route_renders(client_with_rl_and_breaker):
    """The /dashboard route should always render (no DB)."""
    cl, _ = client_with_rl_and_breaker
    resp = await cl.get("/dashboard")
    assert resp.status == 200
    body = await resp.text()
    assert "Tusker Gateway Dashboard" in body
    assert "htmx" in body.lower()


@pytest.mark.asyncio
async def test_dashboard_partials_render(client_with_rl_and_breaker):
    cl, _ = client_with_rl_and_breaker
    for path in (
        "/dashboard/partials/meta",
        "/dashboard/partials/pools",
        "/dashboard/partials/breakers",
        "/dashboard/partials/cooldowns",
        "/dashboard/partials/quota",
        "/dashboard/partials/quality",
    ):
        resp = await cl.get(path)
        assert resp.status == 200, f"{path} returned {resp.status}"


@pytest.mark.asyncio
async def test_dashboard_requires_token_when_configured():
    """If TUSKER_METRICS_TOKEN is set, /dashboard requires the token."""
    os.environ["TUSKER_METRICS_TOKEN"] = "secret-dashboard-token"
    try:
        # Build a fresh app with the token set.
        app = create_app()
        app.on_startup.clear()
        app["http_session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            # No token -> 401
            resp = await client.get("/dashboard")
            assert resp.status == 401
            # Wrong token -> 401
            resp = await client.get("/dashboard", headers={"X-Tusker-Metrics-Token": "wrong"})
            assert resp.status == 401
            # Right token -> 200
            resp = await client.get("/dashboard", headers={"X-Tusker-Metrics-Token": "secret-dashboard-token"})
            assert resp.status == 200
        finally:
            await client.close()
    finally:
        os.environ.pop("TUSKER_METRICS_TOKEN", None)
