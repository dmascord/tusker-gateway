"""Tests for model listing and auth."""
from __future__ import annotations

import pytest
from .conftest import HEADERS_AUTH, HEADERS_NO_AUTH


@pytest.mark.asyncio
async def test_models_requires_auth(client):
    resp = await client.get("/v1/models", headers=HEADERS_NO_AUTH)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_models_list(client):
    resp = await client.get("/v1/models", headers=HEADERS_AUTH)
    assert resp.status == 200
    data = await resp.json()
    assert data["object"] == "list"
    models = data["data"]
    ids = [m["id"] for m in models]
    assert "hermes-code" in ids
    assert "hermes-privacy" in ids
    assert "hermes-premium" in ids


@pytest.mark.asyncio
async def test_models_accepts_x_api_key(client):
    resp = await client.get("/v1/models", headers={"x-api-key": "sk-secret-dev"})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_models_include_legacy_compatibility_ids(client):
    resp = await client.get("/v1/models", headers=HEADERS_AUTH)
    assert resp.status == 200
    ids = {item["id"] for item in (await resp.json())["data"]}
    assert "hermes-gateway/hermes-balanced" in ids
    assert "github-copilot-enterprise/gpt-5.5" in ids
