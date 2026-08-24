"""Tests for pools, selection, and cooldown logic."""
from __future__ import annotations

import os
import tempfile

from tusker_gateway.config import PoolConfig
from tusker_gateway.cooldown import CooldownTracker, _cooldown_seconds_for_429
from tusker_gateway.pools import PoolManager


def test_pool_selection_logic():
    # Use real providers from DEFAULT_PROVIDER_REGISTRY (pools require known providers).
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[
                        {"provider": "groq", "model": "m1"},
                        {"provider": "openai", "model": "m2"},
                    ],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
            # Bearer-kind providers are dropped from pools without keys.
            "provider_api_keys": {"groq": "k-groq", "openai": "k-openai"},
        }
        mgr = PoolManager(config)
        sel1 = mgr.select("test")
        assert sel1 in [("groq", "m1"), ("openai", "m2")], f"got {sel1}"
        sel2 = mgr.select("test", session_id="s1")
        sel3 = mgr.select("test", session_id="s1")
        assert sel2 == sel3, f"stickiness broken: {sel2} vs {sel3}"


def test_unkeyed_bearer_provider_soft_fails():
    """A bearer-kind provider with no API key is dropped from the pool at
    build time instead of preventing startup — the pod stays up, the
    provider just doesn't participate in selection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[
                        {"provider": "groq", "model": "m1"},          # bearer, no key → dropped
                        {"provider": "openai-codex", "model": "m2"},  # codex kind → exempt
                        {"provider": "local-llm", "model": "m3"},     # local kind → exempt
                        {"provider": "openai", "model": "m4"},        # bearer, has key → kept
                    ],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openai": "k-openai"},
        }
        mgr = PoolManager(config)

        selected = {(s.provider, s.model) for s in mgr.models["test"]}
        assert ("groq", "m1") not in selected
        assert ("openai-codex", "m2") in selected
        assert ("local-llm", "m3") in selected
        assert ("openai", "m4") in selected

        # Dropped entries stay visible via status for diagnosis.
        unkeyed = {(e["provider"], e["model"]) for e in mgr.status()["test"]["unkeyed_entries"]}
        assert ("groq", "m1") in unkeyed


def test_cooldown_parsing():
    assert _cooldown_seconds_for_429({"headers": {"Retry-After": "10"}}) == 10
    # "this week" → 7 days = 604800s (rate-limit windows are honored as written)
    assert _cooldown_seconds_for_429({"body": "limit exceeded for this week"}) == 7 * 86400
    # "this month" → 30 days
    assert _cooldown_seconds_for_429({"body": "limit exceeded for this month"}) == 30 * 86400
    # "50/day" → 86400/50 = 1728s between requests
    assert _cooldown_seconds_for_429({"body": "reached 50/day limit"}) == 86400 / 50
    # "rate limited" generic → 60s fallback
    assert _cooldown_seconds_for_429({"body": "rate limited"}) == 60


def test_cooldown_tracker():
    tracker = CooldownTracker()
    assert not tracker.is_cooldown("p1", "m1")
    tracker.cooldown("p1", "m1", 10)
    assert tracker.is_cooldown("p1", "m1")
    assert tracker.is_cooldown("p1", "other")
