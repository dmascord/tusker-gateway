"""Tests for provider capacity protection and usage accounting."""
from __future__ import annotations

import json

import pytest

from tusker_gateway.cooldown import CooldownTracker
from tusker_gateway.errors import ProviderCapacityError
from tusker_gateway.passthrough import _stream_error_from_frame
from tusker_gateway.persistent_cooldown import PersistentCooldownStore
from tusker_gateway.provider_usage import (
    ProviderUsageDB,
    capacity_controller,
    capacity_group_for_route,
)


def test_nvidia_routes_share_one_capacity_group():
    assert capacity_group_for_route("nvidia", "meta/llama") == "nvidia"
    assert capacity_group_for_route(
        "openrouter", "nvidia/nemotron-3:free"
    ) == "nvidia"
    assert capacity_group_for_route("openrouter", "openai/gpt-oss") is None


def test_capacity_controller_fails_fast_at_local_limit(monkeypatch):
    monkeypatch.setenv("TUSKER_NVIDIA_MAX_CONCURRENT", "1")
    controller = capacity_controller()
    controller.reset()

    first = controller.acquire("nvidia")
    assert first is not None
    assert controller.acquire("nvidia") is None
    assert controller.snapshot()["nvidia"]["rejections"] == 1

    first.release()
    second = controller.acquire("nvidia")
    assert second is not None
    second.release()


def test_usage_ledger_aggregates_daily_provider_counters(tmp_path):
    db = ProviderUsageDB(str(tmp_path / "provider_usage.db"))
    db.record(
        provider="openrouter",
        model="nvidia/nemotron-3:free",
        group="nvidia",
        success=True,
        prompt_tokens=12,
        completion_tokens=8,
    )
    db.record(
        provider="openrouter",
        model="nvidia/nemotron-3:free",
        group="nvidia",
        success=False,
        capacity_rejected=True,
    )

    nvidia = db.status()["groups"]["nvidia"]
    assert nvidia == {
        "requests": 2,
        "successes": 1,
        "failures": 1,
        "capacity_rejections": 1,
        "prompt_tokens": 12,
        "completion_tokens": 8,
    }


def test_capacity_sse_error_is_classified_and_contains_no_execution_path():
    payload = {
        "error": {
            "code": 502,
            "message": "Upstream error from Nvidia: ResourceExhausted: Worker local total request limit reached (16/16)",
        }
    }
    error = _stream_error_from_frame(
        f"data: {json.dumps(payload)}\n\n".encode(),
        provider="openrouter",
        model="nvidia/nemotron-3:free",
    )

    assert isinstance(error, ProviderCapacityError)
    assert error.capacity_group == "nvidia"
    assert error.upstream_status == 502
    assert error.capacity_rejected is False


def test_persistent_capacity_group_cooldown_hydrates(tmp_path):
    store = PersistentCooldownStore(tmp_path / "cooldowns.db")
    store.record_group("nvidia", 60.0)
    tracker = CooldownTracker()

    assert store.hydrate_groups(tracker) == 1
    assert tracker.is_group_cooldown("nvidia")
    assert tracker.is_cooldown("openrouter", "nvidia/nemotron-3:free")


@pytest.mark.asyncio
async def test_local_capacity_error_is_client_safe(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from tusker_gateway.passthrough import PassthroughClient
    from tusker_gateway.quality import QualityDB

    monkeypatch.setenv("TUSKER_NVIDIA_MAX_CONCURRENT", "1")
    controller = capacity_controller()
    controller.reset()
    config = {
        "api_keys": ["gateway-test"],
        "provider_api_keys": {"nvidia": "nvidia-test"},
        "codex_credentials": [],
        "quality_db_path": str(tmp_path / "quality.db"),
    }
    client = PassthroughClient(
        config,
        QualityDB(config["quality_db_path"]),
        MagicMock(),
    )
    lease = controller.acquire("nvidia")
    assert lease is not None
    try:
        with pytest.raises(ProviderCapacityError) as caught:
            client._reserve_capacity("nvidia", "meta/llama")
    finally:
        lease.release()

    assert caught.value.capacity_rejected is True
    assert "ResourceExhausted" not in caught.value.message
