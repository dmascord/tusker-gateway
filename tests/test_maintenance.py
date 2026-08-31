"""Tests for bounded background qualification maintenance."""
from __future__ import annotations

import pytest

from tusker_gateway.maintenance import _maintenance_pools, run_maintenance_cycle


def test_maintenance_pools_use_stable_order_and_include_custom_pools(monkeypatch):
    monkeypatch.delenv("TUSKER_QUALIFICATION_MAINTENANCE_POOLS", raising=False)

    assert _maintenance_pools(
        {"pools": {"swarm": object(), "code": object(), "custom": object()}}
    ) == ("code", "swarm", "custom")


@pytest.mark.asyncio
async def test_maintenance_cycle_returns_safe_summary(monkeypatch, tmp_path):
    async def fake_qualification(**kwargs):
        assert kwargs["pool_name"] == "code"
        assert kwargs["max_concurrency"] == 1
        assert kwargs["ignore_cooldowns"] is False
        return [
            {"status": "passed"},
            {"status": "unavailable"},
        ]

    monkeypatch.setattr(
        "tusker_gateway.maintenance.load_config",
        lambda: {"quality_db_path": str(tmp_path / "quality.db")},
    )
    monkeypatch.setattr(
        "tusker_gateway.maintenance.run_qualification",
        fake_qualification,
    )

    summary = await run_maintenance_cycle(pool_name="code", limit=2)

    assert summary == {
        "pool": "code",
        "tested": 2,
        "passed": 1,
        "failed": 1,
        "purged_cooldowns": 0,
    }
