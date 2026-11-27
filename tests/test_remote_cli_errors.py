"""Regression: worker error classification must survive the HTTP proxy hop.

`RemoteCLIAdapter` used to flatten every worker >=400 response to
`worker_error`, so a retired model never got its 410 permanent-failure mark
and the dead auto-discovered route kept re-entering rotation.
See docs/incidents/2026-11-27-auto-discovered-model-worker-error.md.
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderModelRetiredError,
    RateLimitError,
)
from tusker_gateway.provider_adapters.remote_cli import RemoteCLIAdapter


def _app(handler):
    app = web.Application()
    app.router.add_post("/worker/chat", handler)
    return app


async def _worker_exception(handler) -> Exception:
    client = TestClient(TestServer(_app(handler)))
    await client.start_server()
    try:
        adapter = RemoteCLIAdapter(
            "opencode-cli", str(client.make_url("/")).rstrip("/")
        )
        try:
            await adapter.chat(
                provider="opencode-cli", model="seaglass-alpha-free",
                messages=[{"role": "user", "content": "hi"}],
            )
        except Exception as exc:
            return exc
        raise AssertionError("expected adapter.chat to raise")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_worker_model_retired_error_round_trips_to_gateway():
    async def handler(_request):
        return web.json_response(
            {"error": {
                "message": "OpenCode CLI rejected the model as deprecated or unavailable",
                "code": "model_retired",
                "upstream_status": 410,
            }},
            status=410,
        )

    exc = await _worker_exception(handler)
    assert isinstance(exc, ProviderModelRetiredError)
    assert exc.code == "model_retired"
    assert exc.upstream_status == 410


@pytest.mark.asyncio
async def test_worker_retired_error_marks_route_for_catalog_prune():
    """The whole chain must reach the permanent-failure marker."""
    from tusker_gateway.cooldown import clear_permanently_failed, is_permanently_failed
    from tusker_gateway.endpoints import _mark_permanently_failed

    async def handler(_request):
        return web.json_response(
            {"error": {
                "message": "Model unavailable: opencode/seaglass-alpha-free",
                "code": "model_retired",
                "upstream_status": 410,
            }},
            status=410,
        )

    route = ("opencode-cli", "seaglass-alpha-free")
    clear_permanently_failed(*route)
    try:
        exc = await _worker_exception(handler)
        _mark_permanently_failed(exc, *route)
        assert is_permanently_failed(*route)
    finally:
        clear_permanently_failed(*route)


@pytest.mark.asyncio
async def test_worker_auth_error_round_trips_to_gateway():
    async def handler(_request):
        return web.json_response(
            {"error": {
                "message": "OpenCode CLI credential was rejected; rotate the provider API key",
                "code": "provider_auth_failed",
                "upstream_status": 401,
            }},
            status=401,
        )

    exc = await _worker_exception(handler)
    assert isinstance(exc, ProviderAuthError)
    assert exc.code == "provider_auth_failed"
    assert exc.upstream_status == 401


@pytest.mark.asyncio
async def test_worker_rate_limit_error_keeps_retry_window():
    async def handler(_request):
        return web.json_response(
            {"error": {
                "message": "OpenCode CLI rate limit exceeded; retry shortly.",
                "code": "rate_limit_exceeded",
                "body": "Try again in 8 seconds.",
                "headers": {"Retry-After": "8"},
            }},
            status=429,
        )

    exc = await _worker_exception(handler)
    assert isinstance(exc, RateLimitError)
    assert exc.code == "rate_limit_exceeded"
    assert exc.body == "Try again in 8 seconds."
    assert exc.headers == {"Retry-After": "8"}


@pytest.mark.asyncio
async def test_unparseable_worker_error_stays_generic():
    async def handler(_request):
        return web.Response(status=502, text="worker exploded")

    exc = await _worker_exception(handler)
    assert type(exc) is ProviderError
    assert exc.code == "worker_error"
    assert exc.upstream_status == 502
    assert "worker exploded" in str(exc)
