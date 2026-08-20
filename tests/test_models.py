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
