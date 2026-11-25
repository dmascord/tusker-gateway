"""Behavioural coverage for GET /v1/provider-health.

The endpoint exists so a client can learn pool health without the gateway
impersonating an upstream vendor. These tests pin the two properties that
make it trustworthy: a provider with no cooldown is reported as ready, and a
provider whose credentials are parked on a long cooldown is reported as
degraded rather than partially used.
"""
from __future__ import annotations

import asyncio

from tusker_gateway.provider_health import provider_health_payload


def _payload(cooldowns=None, capacity=None, usage=None, now=1_700_000_000.0):
    return provider_health_payload(
        cooldowns=cooldowns or {},
        capacity=capacity or {},
        usage_groups=usage or {},
        now=now,
    )


def test_healthy_provider_is_ready_even_without_cooldowns():
    """Absence of a cooldown is the healthy signal, not absence of a row."""
    out = _payload(
        usage={"apim": {"requests": 10, "successes": 10, "failures": 0}},
    )
    row = next(r for r in out["providers"] if r["provider"] == "apim")
    assert row["ready"] is True
    assert row["cooling_routes"] == 0
    assert row["success_rate_today"] == 1.0
    assert out["summary"] == {"providers": 1, "ready": 1, "degraded": 0}


def test_cooling_provider_is_degraded_and_reports_next_reset():
    out = _payload(
        cooldowns={
            "models": [
                {"provider": "openai-codex", "model": "a", "seconds_remaining": 90.0},
                {"provider": "openai-codex", "model": "b", "seconds_remaining": 90.0},
                {"provider": "openai-codex", "model": "c", "seconds_remaining": 300.0},
            ]
        },
    )
    row = next(r for r in out["providers"] if r["provider"] == "openai-codex")
    assert row["ready"] is False
    assert row["cooling_routes"] == 2  # two distinct windows, not three models
    assert row["next_reset_seconds"] == 90.0
    assert out["summary"]["degraded"] == 1


def test_multiday_cooldown_counts_as_degraded_not_fractional():
    """A credential parked for days is out of service, not 'partly used'."""
    out = _payload(
        cooldowns={
            "providers": [
                {"provider": "alibaba", "seconds_remaining": 1_340_488.6},
            ]
        },
    )
    row = next(r for r in out["providers"] if r["provider"] == "alibaba")
    assert row["ready"] is False
    assert row["cooling_routes"] == 1


def test_capacity_is_included_when_present():
    out = _payload(
        capacity={
            "mlx-mac": {
                "active": 1,
                "limit": 2,
                "queued": 3,
                "rejections": 0,
                "queue_depth": 8,
            }
        },
    )
    row = next(r for r in out["providers"] if r["provider"] == "mlx-mac")
    assert row["capacity"] == {"active": 1, "limit": 2, "queued": 3, "rejections": 0}


def test_payload_contains_no_secret_material():
    """The endpoint is client-facing; it must never leak credential detail."""
    out = _payload(
        cooldowns={
            "models": [
                {"provider": "openai-codex", "model": "m", "seconds_remaining": 5.0}
            ]
        },
        usage={"openai-codex": {"requests": 3, "successes": 1, "failures": 2}},
    )
    blob = repr(out)
    for forbidden in ("token", "secret", "bearer", "api_key", "password", "http"):
        assert forbidden not in blob.lower()


def test_zero_usage_reports_unknown_rate_not_zero():
    """No requests is 'unknown', never a misleading 0% success rate."""
    out = _payload(usage={"quiet": {"requests": 0, "successes": 0, "failures": 0}})
    row = next(r for r in out["providers"] if r["provider"] == "quiet")
    assert row["success_rate_today"] is None


def test_handler_returns_the_payload(monkeypatch):
    """The aiohttp handler wraps the pure builder without reshaping it."""
    from tusker_gateway import provider_health

    monkeypatch.setattr(
        provider_health, "capacity_controller", lambda: _Stub({}), raising=True
    )
    monkeypatch.setattr(
        provider_health, "global_tracker", lambda: _Stub({}), raising=True
    )

    response = asyncio.run(
        provider_health.provider_health_handler(_DummyRequest())
    )
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.body.decode()
    assert '"summary"' in body and '"providers"' in body


class _Stub:
    def __init__(self, value):
        self._value = value

    def snapshot(self):
        return self._value

    def status(self):
        return {"groups": {}}


class _DummyRequest:
    app: dict = {}
