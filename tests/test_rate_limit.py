"""Unit tests for the per-key rate limiter (Release 2)."""
from __future__ import annotations

import os
import time

import pytest

from tusker_gateway.rate_limit import (
    RateLimitConfig,
    RateLimitPolicy,
    RateLimiter,
    _key_fingerprint,
    load_rate_limit_config_from_env,
)


@pytest.fixture
def tmp_rl_path(tmp_path):
    return os.path.join(str(tmp_path), "rl.db")


def _tracker(path: str, policies: dict[str, RateLimitPolicy], default: RateLimitPolicy | None = None) -> RateLimiter:
    return RateLimiter(RateLimitConfig(enabled=True, path=path, policies=policies, default_policy=default))


def test_disabled_allows_everything(tmp_rl_path):
    rl = RateLimiter(RateLimitConfig(enabled=False, path=tmp_rl_path))
    for _ in range(1000):
        assert rl.check("k").allowed


def test_first_call_starts_with_full_burst(tmp_rl_path):
    rl = _tracker(tmp_rl_path, {_key_fingerprint("k"): RateLimitPolicy(rate_per_sec=1.0, burst=10.0)})
    d = rl.check("k")
    assert d.allowed
    assert d.remaining == 9


def test_burst_then_block(tmp_rl_path):
    rl = _tracker(tmp_rl_path, {_key_fingerprint("k"): RateLimitPolicy(rate_per_sec=1.0, burst=3.0, cost_per_request=1.0)})
    assert rl.check("k").allowed
    assert rl.check("k").allowed
    assert rl.check("k").allowed
    d = rl.check("k")
    assert not d.allowed
    assert d.retry_after > 0
    assert rl.stats_snapshot()["blocked"] == 1


def test_refill_over_time(tmp_rl_path):
    rl = _tracker(tmp_rl_path, {_key_fingerprint("k"): RateLimitPolicy(rate_per_sec=100.0, burst=5.0)})
    for _ in range(5):
        rl.check("k")
    assert not rl.check("k").allowed
    time.sleep(0.05)  # ~5 tokens added
    assert rl.check("k").allowed


def test_persistence_across_instances(tmp_rl_path):
    fp = _key_fingerprint("k")
    a = _tracker(tmp_rl_path, {fp: RateLimitPolicy(rate_per_sec=1.0, burst=10.0)})
    for _ in range(5):
        a.check("k")
    # New instance — same DB.
    b = _tracker(tmp_rl_path, {fp: RateLimitPolicy(rate_per_sec=1.0, burst=10.0)})
    # Should have 5 tokens left from previous session.
    assert b.check("k").allowed
    assert b.check("k").allowed
    assert b.check("k").allowed
    assert b.check("k").allowed
    assert b.check("k").allowed
    # 6th call: only 5 left, blocked.
    d = b.check("k")
    assert not d.allowed


def test_default_policy_applies_to_unknown_key(tmp_rl_path):
    rl = _tracker(
        tmp_rl_path,
        policies={_key_fingerprint("vip"): RateLimitPolicy(rate_per_sec=100.0, burst=100.0)},
        default=RateLimitPolicy(rate_per_sec=1.0, burst=2.0),
    )
    # Unknown key uses default policy.
    assert rl.check("unknown").allowed
    assert rl.check("unknown").allowed
    assert not rl.check("unknown").allowed


def test_unknown_key_without_default_allowed(tmp_rl_path):
    rl = _tracker(tmp_rl_path, policies={_key_fingerprint("vip"): RateLimitPolicy(rate_per_sec=1.0, burst=1.0)})
    # No default -> unknown keys always allowed.
    for _ in range(100):
        assert rl.check("unknown").allowed


def test_custom_cost_per_request(tmp_rl_path):
    fp = _key_fingerprint("k")
    rl = _tracker(tmp_rl_path, {fp: RateLimitPolicy(rate_per_sec=1.0, burst=10.0, cost_per_request=5.0)})
    assert rl.check("k").allowed  # 10 -> 5
    assert rl.check("k").allowed  # 5 -> 0
    assert not rl.check("k").allowed  # 0 - 5 < 0


def test_snapshot(tmp_rl_path):
    fp = _key_fingerprint("k")
    rl = _tracker(tmp_rl_path, {fp: RateLimitPolicy(rate_per_sec=1.0, burst=10.0)})
    rl.check("k")
    snap = rl.snapshot()
    assert fp in snap
    assert snap[fp]["tokens"] < 10.0


def test_load_config_from_env_defaults():
    cfg = load_rate_limit_config_from_env(env={})
    assert cfg.enabled is False
    assert cfg.policies == {}


def test_load_config_from_env_json():
    import json
    fp = _key_fingerprint("k1")
    raw = json.dumps({fp: {"rate_per_sec": 5, "burst": 20}})
    cfg = load_rate_limit_config_from_env(env={
        "TUSKER_RATELIMIT_ENABLED": "1",
        "TUSKER_RATELIMIT_JSON": raw,
    })
    assert cfg.enabled
    assert fp in cfg.policies
    assert cfg.policies[fp].rate_per_sec == 5
    assert cfg.policies[fp].burst == 20


def test_load_config_with_default_only():
    cfg = load_rate_limit_config_from_env(env={
        "TUSKER_RATELIMIT_DEFAULT_RATE": "3",
        "TUSKER_RATELIMIT_DEFAULT_BURST": "10",
    })
    assert cfg.default_policy is not None
    assert cfg.default_policy.rate_per_sec == 3
    assert cfg.default_policy.burst == 10


def test_key_fingerprint_deterministic():
    assert _key_fingerprint("k1") == _key_fingerprint("k1")
    assert _key_fingerprint("k1") != _key_fingerprint("k2")
