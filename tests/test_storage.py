"""Tests for the SQLite/PostgreSQL state target selection."""
from __future__ import annotations

from tusker_gateway.storage import (
    StorageUnavailableError,
    is_postgres_target,
    shared_database,
    shared_state_target,
    state_degraded_mode,
    state_database_url,
    storage_status,
)
from tusker_gateway.rate_limit import RateLimitConfig, RateLimitPolicy, RateLimiter


def test_state_database_url_prefers_explicit_state_name():
    assert state_database_url({
        "TUSKER_STATE_DATABASE_URL": "postgresql://state",
        "TUSKER_DATABASE_URL": "postgresql://alias",
    }) == "postgresql://state"


def test_state_database_url_accepts_compatibility_alias():
    assert state_database_url({"TUSKER_DATABASE_URL": "postgresql://alias"}) == (
        "postgresql://alias"
    )


def test_state_degraded_mode_accepts_only_known_policies():
    assert state_degraded_mode({"TUSKER_STATE_DEGRADED_MODE": "critical"}) == "critical"
    assert state_degraded_mode({"TUSKER_STATE_DEGRADED_MODE": "unexpected"}) == "advisory"


def test_shared_state_target_preserves_memory_databases(monkeypatch):
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "postgresql://state")
    assert shared_state_target(":memory:") == ":memory:"


def test_shared_state_target_uses_postgres_when_configured(monkeypatch):
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "postgresql://state")
    assert shared_state_target("/tmp/quality.db") == "postgresql://state"
    assert is_postgres_target("postgres://state")
    assert is_postgres_target("postgresql://state")
    assert not is_postgres_target("/tmp/quality.db")


def test_advisory_postgres_outage_uses_noop_process_local_fallback(monkeypatch):
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "postgresql://state")
    database = shared_database("/tmp/ignored-quality.db")

    with database.connection() as connection:
        assert connection.execute("SELECT 1").fetchone() is None
        connection.commit()

    status = storage_status()
    assert status["configured"] is True
    assert status["degraded"] is True
    assert status["backend"] == "degraded"


def test_critical_postgres_outage_raises_without_sqlite_fallback(monkeypatch):
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "postgresql://state")
    database = shared_database("/tmp/ignored-rate-limit.db", fallback_policy="critical")

    try:
        with database.connection():
            pass
    except StorageUnavailableError:
        pass
    else:
        raise AssertionError("critical state unexpectedly used a fallback connection")


def test_rate_limiter_stays_enabled_and_fails_closed_when_postgres_is_down(monkeypatch):
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "postgresql://state")
    limiter = RateLimiter(
        RateLimitConfig(
            enabled=True,
            path="/tmp/ignored-rate-limit.db",
            default_policy=RateLimitPolicy(),
        )
    )
    assert limiter._config.enabled is True
    try:
        limiter.check("test-key")
    except StorageUnavailableError:
        pass
    else:
        raise AssertionError("rate limiter did not fail closed")
