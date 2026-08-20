"""Tests for pools, selection, and cooldown logic."""
from __future__ import annotations

import os
import tempfile

from tusker_gateway.config import PoolConfig
from tusker_gateway.cooldown import CooldownTracker, _cooldown_seconds_for_429
from tusker_gateway.pools import PoolManager


def test_pool_selection_logic():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[
                        {"provider": "p1", "model": "m1"},
                        {"provider": "p2", "model": "m2"},
                    ],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
        }
        mgr = PoolManager(config)
        sel1 = mgr.select("test")
        assert sel1 in [("p1", "m1"), ("p2", "m2")]
        sel2 = mgr.select("test", session_id="s1")
        sel3 = mgr.select("test", session_id="s1")
        assert sel2 == sel3


def test_cooldown_parsing():
    assert _cooldown_seconds_for_429({"headers": {"Retry-After": "10"}}) == 10
    assert _cooldown_seconds_for_429({"body": "limit exceeded for this week"}) == 3600
    assert _cooldown_seconds_for_429({"body": "reached 50/day limit"}) == 3600
    assert _cooldown_seconds_for_429({"body": "rate limited"}) == 60


def test_cooldown_tracker():
    tracker = CooldownTracker()
    assert not tracker.is_cooldown("p1", "m1")
    tracker.cooldown("p1", "m1", 10)
    assert tracker.is_cooldown("p1", "m1")
    assert tracker.is_cooldown("p1", "other")
