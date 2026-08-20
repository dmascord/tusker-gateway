"""Unit tests for the per-key budget tracker (Release 1)."""
from __future__ import annotations

import os

import pytest

from tusker_gateway.budget import (
    BudgetCaps,
    BudgetConfig,
    BudgetTracker,
    _key_fingerprint,
    load_budget_config_from_env,
)


@pytest.fixture
def tmp_budget_path(tmp_path):
    return os.path.join(str(tmp_path), "budget.db")


def _tracker(enabled: bool = True, path: str = "", **caps_kwargs) -> BudgetTracker:
    path = path or os.path.join("/tmp", "budget_test_default.db")
    if os.path.exists(path):
        os.remove(path)
    caps = BudgetCaps(**caps_kwargs)
    fp = _key_fingerprint("k1")
    return BudgetTracker(BudgetConfig(
        enabled=enabled, path=path,
        caps={fp: caps},
    ))


def test_disabled_tracker_allows_everything(tmp_budget_path):
    b = BudgetTracker(BudgetConfig(enabled=False, path=tmp_budget_path))
    for _ in range(100):
        assert b.check("k1", "code", 1000).allowed


def test_record_then_block(tmp_budget_path):
    b = _tracker(path=tmp_budget_path, daily_tokens=1000)
    assert b.check("k1", "code", 500).allowed
    b.record("k1", "code", 500)
    d = b.check("k1", "code", 600)
    assert not d.allowed
    assert d.cap_name == "daily"
    assert d.cap == 1000
    assert "1000" in (d.reason or "")


def test_refund_restores_capacity(tmp_budget_path):
    b = _tracker(path=tmp_budget_path, daily_tokens=1000)
    b.record("k1", "code", 800)
    # Now 200 left; refund 300 to give us 500 headroom again.
    b.refund("k1", "code", 300)
    assert b.check("k1", "code", 500).allowed


def test_monthly_cap_independent_from_daily(tmp_budget_path):
    b = _tracker(path=tmp_budget_path, daily_tokens=10_000, monthly_tokens=2000)
    # Daily allows, monthly caps.
    assert b.check("k1", "code", 1500).allowed
    b.record("k1", "code", 1500)
    d = b.check("k1", "code", 600)
    assert not d.allowed
    assert d.cap_name == "monthly"


def test_per_pool_cap(tmp_budget_path):
    b = _tracker(path=tmp_budget_path, per_pool_tokens={"code": 1000, "privacy": 500})
    assert b.check("k1", "code", 600).allowed
    b.record("k1", "code", 600)
    d = b.check("k1", "code", 500)
    assert not d.allowed
    assert d.cap_name == "pool:code"
    # Other pool still has its own cap.
    assert b.check("k1", "privacy", 400).allowed


def test_global_daily_cap_applies_to_all_keys(tmp_budget_path):
    cfg = BudgetConfig(
        enabled=True, path=tmp_budget_path,
        caps={"any": BudgetCaps(daily_tokens=10_000)},
        global_daily_tokens=2000,
    )
    b = BudgetTracker(cfg)
    b.record("any", "code", 1500)
    d = b.check("any", "code", 600)
    assert not d.allowed
    assert d.cap_name == "global_daily"


def test_unknown_key_no_per_key_caps(tmp_budget_path):
    """Keys without configured caps should still pass through cleanly."""
    b = _tracker(path=tmp_budget_path, daily_tokens=100)
    assert b.check("unknown", "code", 500).allowed


def test_rolling_window_resets(tmp_budget_path):
    """Old entries outside the rolling window don't count."""
    b = _tracker(path=tmp_budget_path, daily_tokens=1000)
    b.record("k1", "code", 800)
    # Manually backdate the row by injecting a row with an old period_start.
    import sqlite3, time
    conn = sqlite3.connect(tmp_budget_path)
    old_start = time.time() - BudgetTracker.DAILY_WINDOW - 10
    conn.execute(
        "UPDATE usage SET period_start = ? WHERE period = 'daily'",
        (old_start,),
    )
    conn.commit()
    conn.close()
    assert b.check("k1", "code", 500).allowed


def test_stats_snapshot(tmp_budget_path):
    b = _tracker(path=tmp_budget_path, daily_tokens=100)
    b.record("k1", "code", 80)
    b.check("k1", "code", 50)  # blocked
    s = b.stats_snapshot()
    assert s["blocks_daily"] == 1
    assert s["records"] == 1
    assert s["refunds"] == 0
    b.refund("k1", "code", 30)
    assert b.stats_snapshot()["refunds"] == 1


def test_load_budget_config_from_env_defaults():
    cfg = load_budget_config_from_env(env={})
    assert cfg.enabled is False
    assert cfg.caps == {}


def test_load_budget_config_from_env_json():
    import json
    fp = _key_fingerprint("k1")
    raw = json.dumps({fp: {"daily_tokens": 1000, "per_pool_tokens": {"code": 500}}})
    cfg = load_budget_config_from_env(env={
        "TUSKER_BUDGETS_ENABLED": "1",
        "TUSKER_BUDGETS_JSON": raw,
    })
    assert cfg.enabled
    assert fp in cfg.caps
    assert cfg.caps[fp].daily_tokens == 1000
    assert cfg.caps[fp].per_pool_tokens == {"code": 500}


def test_load_budget_config_handles_malformed_json():
    cfg = load_budget_config_from_env(env={
        "TUSKER_BUDGETS_JSON": "{not json",
    })
    assert cfg.caps == {}


def test_key_fingerprint_deterministic():
    assert _key_fingerprint("k1") == _key_fingerprint("k1")
    assert _key_fingerprint("k1") != _key_fingerprint("k2")
    assert len(_key_fingerprint("k1")) == 32


def test_usage_snapshot(tmp_budget_path):
    b = _tracker(path=tmp_budget_path, daily_tokens=10_000)
    b.record("k1", "code", 100)
    b.record("k1", "privacy", 50)
    snap = b.usage_snapshot("k1")
    assert "daily" in snap or any("pool:" in k for k in snap)
