"""Unit tests for the circuit breaker (Release 2)."""
from __future__ import annotations

import os
import time

import pytest

from tusker_gateway.circuit_breaker import (
    BreakerConfig,
    BreakerPolicy,
    BreakerState,
    CircuitBreaker,
    load_circuit_config_from_env,
)


@pytest.fixture
def tmp_breaker_path(tmp_path):
    return os.path.join(str(tmp_path), "circuit.db")


def _cfg(path: str, **policy_kwargs) -> BreakerConfig:
    return BreakerConfig(
        enabled=True, path=path,
        policy=BreakerPolicy(**policy_kwargs),
    )


def test_disabled_breaker_allows_everything(tmp_breaker_path):
    cb = CircuitBreaker(BreakerConfig(enabled=False, path=tmp_breaker_path))
    for _ in range(100):
        d = cb.check("p", "m")
        assert d.allowed and d.state == BreakerState.CLOSED


def test_consecutive_failures_trip(tmp_breaker_path):
    cb = CircuitBreaker(_cfg(tmp_breaker_path, consecutive_failures=3))
    assert cb.check("p", "m").allowed
    cb.record_failure("p", "m")
    cb.record_failure("p", "m")
    assert cb.check("p", "m").allowed  # 2 < 3
    cb.record_failure("p", "m")
    d = cb.check("p", "m")
    assert not d.allowed
    assert d.state == BreakerState.OPEN
    assert cb.stats_snapshot()["trips"] == 1


def test_success_resets_consecutive_count(tmp_breaker_path):
    cb = CircuitBreaker(_cfg(tmp_breaker_path, consecutive_failures=3))
    cb.record_failure("p", "m")
    cb.record_failure("p", "m")
    cb.record_success("p", "m")  # resets consecutive_failures to 0
    cb.record_failure("p", "m")
    assert cb.check("p", "m").allowed  # not yet at threshold


def test_window_ratio_trips(tmp_breaker_path):
    cb = CircuitBreaker(_cfg(tmp_breaker_path, consecutive_failures=100, window_size=4, failure_ratio=0.5))
    # The trip check runs on each FAILURE. So sequence: 1 fail, 1 succ, 1 fail, 1 fail.
    # After the 4th call (a failure), window is 3/4 = 0.75 >= 0.5 -> trip.
    cb.record_failure("p", "m")  # 1/1 = 1.0
    cb.record_success("p", "m")  # 1/2 = 0.5
    cb.record_failure("p", "m")  # 2/3 = 0.67
    cb.record_failure("p", "m")  # 3/4 = 0.75 -> trips
    assert not cb.check("p", "m").allowed


def test_open_to_half_open_after_cooldown(tmp_breaker_path):
    cb = CircuitBreaker(_cfg(tmp_breaker_path, consecutive_failures=1, cooldown_secs=0.05))
    cb.record_failure("p", "m")
    assert not cb.check("p", "m").allowed
    time.sleep(0.1)
    d = cb.check("p", "m")
    assert d.allowed
    assert d.state == BreakerState.HALF_OPEN
    assert cb.stats_snapshot()["half_open_probes"] == 1


def test_half_open_probe_success_closes(tmp_breaker_path):
    cb = CircuitBreaker(_cfg(tmp_breaker_path, consecutive_failures=1, cooldown_secs=0.05))
    cb.record_failure("p", "m")
    time.sleep(0.1)
    cb.check("p", "m")  # this transitions to HALF_OPEN and reserves the probe
    cb.release_probe("p", "m")
    cb.record_success("p", "m")
    # State should be CLOSED again.
    assert cb.check("p", "m").allowed
    assert cb.stats_snapshot()["half_open_successes"] == 1


def test_half_open_probe_failure_reopens(tmp_breaker_path):
    cb = CircuitBreaker(_cfg(tmp_breaker_path, consecutive_failures=1, cooldown_secs=0.05))
    cb.record_failure("p", "m")
    time.sleep(0.1)
    cb.check("p", "m")
    cb.release_probe("p", "m")
    cb.record_failure("p", "m")
    d = cb.check("p", "m")
    assert not d.allowed
    assert d.state == BreakerState.OPEN


def test_concurrent_probe_blocked(tmp_breaker_path):
    """While a half-open probe is in flight, additional checks are short-circuited."""
    cb = CircuitBreaker(_cfg(tmp_breaker_path, consecutive_failures=1, cooldown_secs=0.05))
    cb.record_failure("p", "m")
    time.sleep(0.1)
    first = cb.check("p", "m")
    assert first.allowed
    # Now the probe is in flight — second check should be blocked.
    second = cb.check("p", "m")
    assert not second.allowed


def test_short_circuits_counter(tmp_breaker_path):
    cb = CircuitBreaker(_cfg(tmp_breaker_path, consecutive_failures=1, cooldown_secs=60))
    cb.record_failure("p", "m")
    for _ in range(5):
        cb.check("p", "m")
    assert cb.stats_snapshot()["short_circuits"] >= 4


def test_per_provider_override(tmp_breaker_path):
    cb = CircuitBreaker(BreakerConfig(
        enabled=True, path=tmp_breaker_path,
        policy=BreakerPolicy(consecutive_failures=10),
        overrides={"strict": BreakerPolicy(consecutive_failures=2)},
    ))
    cb.record_failure("strict", "m")
    cb.record_failure("strict", "m")
    assert not cb.check("strict", "m").allowed
    cb.record_failure("lenient", "m")
    assert cb.check("lenient", "m").allowed


def test_snapshot(tmp_breaker_path):
    cb = CircuitBreaker(_cfg(tmp_breaker_path, consecutive_failures=2))
    cb.record_failure("p1", "m1")
    cb.record_failure("p1", "m1")
    snap = cb.snapshot()
    assert "p1|m1" in snap
    assert snap["p1|m1"]["state"] == "open"


def test_load_config_from_env_defaults():
    cfg = load_circuit_config_from_env(env={})
    assert cfg.enabled is False


def test_load_config_from_env_overrides():
    cfg = load_circuit_config_from_env(env={
        "TUSKER_CIRCUIT_ENABLED": "true",
        "TUSKER_CIRCUIT_CONSECUTIVE": "7",
        "TUSKER_CIRCUIT_WINDOW": "30",
        "TUSKER_CIRCUIT_RATIO": "0.7",
        "TUSKER_CIRCUIT_COOLDOWN": "120",
    })
    assert cfg.enabled
    assert cfg.policy.consecutive_failures == 7
    assert cfg.policy.window_size == 30
    assert cfg.policy.failure_ratio == 0.7
    assert cfg.policy.cooldown_secs == 120
