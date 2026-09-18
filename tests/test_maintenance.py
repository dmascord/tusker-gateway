"""Tests for bounded background qualification maintenance."""

from __future__ import annotations

import pytest

from tusker_gateway.maintenance import (
    _maintenance_pools,
    _modality_name,
    _modality_pool_order,
    _modality_qualification_enabled,
    run_maintenance_cycle,
    run_modality_maintenance_cycle,
)


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


def test_modality_pool_order_defaults_to_vision_pools(monkeypatch):
    monkeypatch.delenv("TUSKER_MODALITY_QUALIFICATION_POOLS", raising=False)

    assert _modality_pool_order(
        {"pools": {"code": object(), "premium": object(), "swarm": object(), "privacy": object()}}
    ) == ("code", "premium", "swarm")


def test_modality_pool_order_honours_env_override(monkeypatch):
    monkeypatch.setenv("TUSKER_MODALITY_QUALIFICATION_POOLS", "privacy,swarm")

    assert _modality_pool_order(
        {"pools": {"code": object(), "premium": object(), "swarm": object(), "privacy": object()}}
    ) == ("privacy", "swarm")


def test_modality_qualification_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("TUSKER_MODALITY_QUALIFICATION_ENABLED", raising=False)
    monkeypatch.delenv("TUSKER_MODALITY_QUALIFICATION_POOLS", raising=False)

    assert (
        _modality_qualification_enabled({"pools": {"code": object(), "premium": object()}}) is False
    )


def test_modality_qualification_enabled_can_be_toggled(monkeypatch):
    monkeypatch.setenv("TUSKER_MODALITY_QUALIFICATION_ENABLED", "true")
    monkeypatch.delenv("TUSKER_MODALITY_QUALIFICATION_POOLS", raising=False)

    assert (
        _modality_qualification_enabled({"pools": {"code": object(), "premium": object()}}) is True
    )


def test_modality_name_defaults_to_image(monkeypatch):
    monkeypatch.delenv("TUSKER_MODALITY_QUALIFICATION_MODALITY", raising=False)
    assert _modality_name() == "image"


def test_modality_name_honours_env(monkeypatch):
    monkeypatch.setenv("TUSKER_MODALITY_QUALIFICATION_MODALITY", "audio")
    assert _modality_name() == "audio"


@pytest.mark.asyncio
async def test_modality_cycle_returns_safe_summary(monkeypatch):
    captured: dict = {}

    async def fake_run_qualification(**kwargs):
        captured.update(kwargs)
        return [
            {"status": "passed", "provider": "minimax", "model": "MiniMax-M2.7"},
            {"status": "passed", "provider": "minimax", "model": "MiniMax-M2.5"},
            {"status": "unsupported", "provider": "openai-codex", "model": "gpt-5.6-luna"},
            {"status": "unavailable", "provider": "groq", "model": "gpt-oss-20b"},
        ]

    monkeypatch.setenv("API_KEYS", "test-key-1,test-key-2")
    monkeypatch.setattr(
        "tusker_gateway.modality_qualification.run_qualification",
        fake_run_qualification,
    )

    summary = await run_modality_maintenance_cycle(
        pool_names=("code", "premium"),
        input_modality="image",
        limit=4,
        timeout_secs=33.0,
        max_age_secs=3600.0,
    )

    # The wrapper must forward the gated set of arguments to the runner and
    # never opt in to ignore_cooldowns — maintenance must not bypass
    # quarantine. Image is the only modality probed today.
    assert captured["pool_names"] == ["code", "premium"]
    assert captured["input_modality"] == "image"
    assert captured["max_concurrency"] == 1
    assert captured["limit"] == 4
    assert captured["timeout_secs"] == 33.0
    assert captured["max_age_secs"] == 3600.0
    assert captured["ignore_cooldowns"] is False

    assert summary == {
        "modality": "image",
        "pools": "code,premium",
        "tested": 4,
        "passed": 2,
        "unsupported": 1,
        "unavailable": 1,
    }


@pytest.mark.asyncio
async def test_modality_cycle_requires_api_keys(monkeypatch):
    monkeypatch.delenv("API_KEYS", raising=False)

    with pytest.raises(RuntimeError, match="API_KEYS"):
        await run_modality_maintenance_cycle(pool_names=("code",))


@pytest.mark.asyncio
async def test_modality_cycle_forwards_transient_and_delay_kwargs(monkeypatch):
    captured: dict = {}

    async def fake_run_qualification(**kwargs):
        captured.update(kwargs)
        return [
            {"status": "passed", "provider": "minimax", "model": "MiniMax-M2.7"},
        ]

    monkeypatch.setenv("API_KEYS", "test-key-1")
    monkeypatch.setattr(
        "tusker_gateway.modality_qualification.run_qualification",
        fake_run_qualification,
    )

    summary = await run_modality_maintenance_cycle(
        pool_names=("code",),
        transient_max_age_secs=1234.0,
        per_probe_delay_secs=7.5,
    )

    assert captured["transient_max_age_secs"] == 1234.0
    assert captured["per_probe_delay_secs"] == 7.5
    assert summary["passed"] == 1


def test_needs_probe_differentiates_passed_from_unavailable():
    """Cache ``passed``/``unsupported`` for ``max_age_secs`` and
    ``unavailable`` (transient failures) for the shorter
    ``transient_max_age_secs``.
    """
    from types import SimpleNamespace

    from tusker_gateway.modality_qualification import _needs_probe

    now = 1_000_000.0

    class FixedTime:
        def time(self) -> float:
            return now

    # Patch time.time used inside _needs_probe by patching the module import.
    import tusker_gateway.modality_qualification as mq

    monkey = SimpleNamespace(time=lambda: now)
    orig_time, mq.time.time = mq.time.time, monkey.time
    try:
        # A 30-minute-old passed record with 1h transient / 24h max:
        # should NOT need probing — well within both windows.
        record = SimpleNamespace(
            status="passed",
            source="modality_probe",
            probe_version=mq.MODEL_CAPABILITY_PROBE_VERSION,
            checked_at=now - 1800.0,
        )
        assert (
            _needs_probe(record, force=False, max_age_secs=86400.0, transient_max_age_secs=3600.0)
            is False
        )

        # A 30-minute-old unavailable record with 1h transient / 24h max:
        # should NOT need probing — fresh enough on the transient clock.
        record = SimpleNamespace(
            status="unavailable",
            source="modality_probe",
            probe_version=mq.MODEL_CAPABILITY_PROBE_VERSION,
            checked_at=now - 1800.0,
        )
        assert (
            _needs_probe(record, force=False, max_age_secs=86400.0, transient_max_age_secs=3600.0)
            is False
        )

        # A 2-hour-old unavailable record with 1h transient / 24h max:
        # MUST need probing — past the transient window but well within the
        # authoritative max window. Without the split, the runner would
        # wait a full 24h before retrying a recovered provider.
        record = SimpleNamespace(
            status="unavailable",
            source="modality_probe",
            probe_version=mq.MODEL_CAPABILITY_PROBE_VERSION,
            checked_at=now - 7200.0,
        )
        assert (
            _needs_probe(record, force=False, max_age_secs=86400.0, transient_max_age_secs=3600.0)
            is True
        )

        # A 2-hour-old passed record with 1h transient / 24h max:
        # should NOT need probing — the authoritative max is 24h.
        record = SimpleNamespace(
            status="passed",
            source="modality_probe",
            probe_version=mq.MODEL_CAPABILITY_PROBE_VERSION,
            checked_at=now - 7200.0,
        )
        assert (
            _needs_probe(record, force=False, max_age_secs=86400.0, transient_max_age_secs=3600.0)
            is False
        )
    finally:
        mq.time.time = orig_time
