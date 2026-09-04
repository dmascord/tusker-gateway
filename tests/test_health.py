"""Tests for health and status endpoints."""
from __future__ import annotations

import pytest
from .conftest import HEADERS_AUTH, HEADERS_NO_AUTH


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
