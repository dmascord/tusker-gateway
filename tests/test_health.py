"""Tests for health and status endpoints."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from .conftest import HEADERS_AUTH, HEADERS_NO_AUTH
from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY, PoolConfig
from tusker_gateway.health import ready_handler


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_ready(client):
    resp = await client.get("/ready")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


def test_ready_ignores_empty_optional_pool_when_primary_is_usable():
    """An empty optional tier must not evict a healthy primary route."""
    cfg = {
        "providers": DEFAULT_PROVIDER_REGISTRY,
        "pools": {
            "code": PoolConfig(
                "code",
                [{"provider": "local-llm", "model": "primary"}],
                fallback_pools=("premium",),
            ),
            "premium": PoolConfig(
                "premium",
                [{"provider": "removed-provider", "model": "optional"}],
            ),
        },
    }
    manager = SimpleNamespace(
        models={
            "code": [SimpleNamespace(provider="local-llm", model="primary", zdr_ok=True)],
            "premium": [],
        },
        fallback_pools=lambda name: ("premium",) if name == "code" else (),
    )
    response = ready_handler(SimpleNamespace(app={"config": cfg, "pool_manager": manager}))

    assert response.status == 200
    data = json.loads(response.text)
    assert data["primary_route"] == ["code", "premium"]
    assert data["degraded_pools"] == ["premium"]
    assert data["pools"]["code"]["usable"] == 1
    assert data["pools"]["premium"]["usable"] == 0


def test_ready_fails_when_primary_and_fallbacks_are_empty():
    """Readiness still fails when the route clients actually use is empty."""
    cfg = {
        "providers": DEFAULT_PROVIDER_REGISTRY,
        "pools": {
            "code": PoolConfig(
                "code",
                [{"provider": "local-llm", "model": "primary"}],
                fallback_pools=("premium",),
            ),
            "premium": PoolConfig(
                "premium",
                [{"provider": "local-llm", "model": "fallback"}],
            ),
            "privacy": PoolConfig(
                "privacy",
                [{"provider": "local-llm", "model": "private"}],
                zdr=True,
            ),
        },
    }
    manager = SimpleNamespace(
        models={
            "code": [],
            "premium": [],
            "privacy": [
                SimpleNamespace(provider="local-llm", model="private", zdr_ok=True),
            ],
        },
        fallback_pools=lambda name: ("premium",) if name == "code" else (),
    )
    response = ready_handler(SimpleNamespace(app={"config": cfg, "pool_manager": manager}))

    assert response.status == 503
    data = json.loads(response.text)
    assert data["reason"] == "no usable candidates for primary route"
    assert data["primary_route"] == ["code", "premium"]
    assert data["empty_pools"] == ["code", "premium"]


def test_ready_does_not_count_oauth_candidate_without_credentials():
    """An OAuth route without a token pool is not operationally usable."""
    cfg = {
        "providers": DEFAULT_PROVIDER_REGISTRY,
        "credential_pools": {"openai-codex": []},
        "pools": {
            "code": PoolConfig(
                "code",
                [{"provider": "openai-codex", "model": "gpt-test"}],
            ),
        },
    }
    manager = SimpleNamespace(
        models={
            "code": [SimpleNamespace(provider="openai-codex", model="gpt-test", zdr_ok=True)],
        },
        fallback_pools=lambda name: (),
    )
    response = ready_handler(SimpleNamespace(app={"config": cfg, "pool_manager": manager}))

    assert response.status == 503
    data = json.loads(response.text)
    assert data["pools"]["code"]["usable"] == 0


@pytest.mark.asyncio
async def test_status_requires_auth(client):
    resp = await client.get("/status", headers=HEADERS_NO_AUTH)
    assert resp.status == 401
    data = await resp.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_status_authenticated(client):
    resp = await client.get("/status", headers=HEADERS_AUTH)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert "pools" in data
    assert "quality" in data
    assert "enterprise_controls" in data
