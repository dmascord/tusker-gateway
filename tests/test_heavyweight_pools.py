"""Tests for per-pool heavyweight gating in PoolManager.

Verifies the hermes-agent-mirrored tier rules:
- code, privacy pools drop heavyweight entries (cheap-tier rotation)
- premium, swarm pools keep heavyweight entries (paid-tier rotation)
"""
from __future__ import annotations

import tempfile

import pytest

from tusker_gateway.config import PoolConfig
from tusker_gateway.pools import ModelSpec, PoolManager, PREMIUM_POOLS


# ---------------------------------------------------------------------------
# PREMIUM_POOLS constant
# ---------------------------------------------------------------------------


def test_premium_pools_contains_premium_and_swarm():
    assert "premium" in PREMIUM_POOLS
    assert "swarm" in PREMIUM_POOLS


def test_premium_pools_does_not_contain_cheap_tiers():
    assert "code" not in PREMIUM_POOLS
    assert "privacy" not in PREMIUM_POOLS


# ---------------------------------------------------------------------------
# ModelSpec heavy classification
# ---------------------------------------------------------------------------


def test_modelspec_classifies_known_heavy_slug():
    """Slugs in the override set are heavyweight even without per-entry flag."""
    s = ModelSpec.from_dict(
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        default_window=128_000,
        zdr=False,
    )
    assert s.heavyweight is True


def test_modelspec_classifies_known_light_slug():
    s = ModelSpec.from_dict(
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        default_window=128_000,
        zdr=False,
    )
    assert s.heavyweight is False


def test_modelspec_per_entry_override_wins():
    """Per-entry heavyweight flag overrides slug classifier."""
    # Force a normally-light model to be heavy
    s = ModelSpec.from_dict(
        {"provider": "openai-codex", "model": "gpt-5.6-luna", "heavyweight": True},
        default_window=128_000,
        zdr=False,
    )
    assert s.heavyweight is True
    # Force a normally-heavy model to be light
    s = ModelSpec.from_dict(
        {"provider": "openai-codex", "model": "gpt-5.6-sol", "heavyweight": False},
        default_window=128_000,
        zdr=False,
    )
    assert s.heavyweight is False


def test_modelspec_zdr_excludes_heavyweights():
    """ZDR pools always drop heavyweights (existing behaviour preserved)."""
    s = ModelSpec.from_dict(
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        default_window=128_000,
        zdr=True,
    )
    assert s.zdr_ok is True  # light slug OK in ZDR
    s = ModelSpec.from_dict(
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        default_window=128_000,
        zdr=True,
    )
    assert s.zdr_ok is False  # heavy slug dropped in ZDR


# ---------------------------------------------------------------------------
# PoolManager tier rules
# ---------------------------------------------------------------------------


def _make_pool_manager(pool_name: str, models: list[dict], zdr: bool = False) -> PoolManager:
    """Build a PoolManager with a single pool, no quality/cooldown dependencies."""
    cfg = {
        "pools": {pool_name: PoolConfig(name=pool_name, models=models, zdr=zdr)},
        "excluded_providers": [],
        # Real file path: ":memory:" doesn't work because sqlite3.connect(":memory:")
        # creates a per-connection database.
        "quality_db_path": tempfile.mktemp(suffix=".db"),
    }
    return PoolManager(cfg)


def test_code_pool_drops_heavyweights():
    """Code pool = cheap tier. Heavyweight slugs are filtered out of selection."""
    pm = _make_pool_manager("code", [
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},  # heavy
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},  # light
    ])
    selected = pm.select("code")
    assert selected is not None
    assert selected == ("openai-codex", "gpt-5.6-luna")


def test_privacy_pool_drops_heavyweights():
    """Privacy pool = cheap tier + ZDR. Heavy slugs are filtered out."""
    pm = _make_pool_manager("privacy", [
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},  # heavy
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},  # light
    ], zdr=True)
    selected = pm.select("privacy")
    assert selected is not None
    assert selected == ("openai-codex", "gpt-5.6-luna")


def test_privacy_pool_drops_provider_without_zdr_policy():
    """Privacy routing must not use a provider merely because it is keyed."""
    pm = _make_pool_manager("privacy", [
        {"provider": "github-copilot", "model": "gpt-5.6-luna"},
        {"provider": "github-copilot-enterprise", "model": "gpt-5-mini"},
    ], zdr=True)
    selected = pm.select("privacy")
    assert selected == ("github-copilot-enterprise", "gpt-5-mini")


def test_privacy_pool_keeps_provider_with_zdr_policy():
    pm = _make_pool_manager("privacy", [
        {"provider": "github-copilot-enterprise", "model": "gpt-5-mini"},
    ], zdr=True)
    assert pm.models["privacy"][0].zdr_ok is True


def test_premium_pool_keeps_heavyweights():
    """Premium pool = paid tier. Heavy slugs ARE allowed."""
    cfg = {
        "pools": {
            "premium": PoolConfig(
                name="premium",
                models=[
                    {"provider": "openai-codex", "model": "gpt-5.6-sol"},
                    {"provider": "openai-codex", "model": "gpt-5.6-terra"},
                    {"provider": "openai-codex", "model": "gpt-5.6-luna"},
                ],
            ),
        },
        "excluded_providers": [],
        "quality_db_path": tempfile.mktemp(suffix=".db"),
    }
    pm = PoolManager(cfg)
    selected = pm.select("premium")
    assert selected is not None
    # sol is heavy but allowed in premium pool
    assert selected in {
        ("openai-codex", "gpt-5.6-sol"),
        ("openai-codex", "gpt-5.6-terra"),
        ("openai-codex", "gpt-5.6-luna"),
    }


def test_swarm_pool_keeps_heavyweights():
    """Swarm pool = paid tier. Heavy slugs ARE allowed."""
    cfg = {
        "pools": {
            "swarm": PoolConfig(
                name="swarm",
                models=[
                    {"provider": "github-copilot", "model": "gpt-5.5"},  # heavy
                    {"provider": "openai-codex", "model": "gpt-5.6-sol"},  # heavy
                ],
            ),
        },
        "excluded_providers": [],
        "quality_db_path": tempfile.mktemp(suffix=".db"),
    }
    pm = PoolManager(cfg)
    selected = pm.select("swarm")
    assert selected is not None
    assert selected in {
        ("github-copilot", "gpt-5.5"),
        ("openai-codex", "gpt-5.6-sol"),
    }


def test_select_with_heavyweight_ok_override():
    """Caller can override the pool's tier rule by passing heavyweight_ok=True."""
    pm = _make_pool_manager("code", [
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},  # heavy
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
    ])
    # Override: allow heavyweight even in code pool
    selected = pm.select("code", heavyweight_ok=True)
    assert selected in {
        ("openai-codex", "gpt-5.6-sol"),
        ("openai-codex", "gpt-5.6-luna"),
    }


def test_select_with_heavyweight_ok_false_in_premium():
    """Caller can override the pool's tier rule by passing heavyweight_ok=False."""
    cfg = {
        "pools": {
            "premium": PoolConfig(
                name="premium",
                models=[
                    {"provider": "openai-codex", "model": "gpt-5.6-sol"},  # heavy
                    {"provider": "openai-codex", "model": "gpt-5.6-luna"},  # light
                ],
            ),
        },
        "excluded_providers": [],
        "quality_db_path": tempfile.mktemp(suffix=".db"),
    }
    pm = PoolManager(cfg)
    # Override: drop heavyweight even in premium pool
    selected = pm.select("premium", heavyweight_ok=False)
    assert selected == ("openai-codex", "gpt-5.6-luna")


def test_code_pool_with_only_heavyweights_returns_none():
    """If all entries are heavyweight and pool is cheap-tier, no candidate."""
    pm = _make_pool_manager("code", [
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},  # heavy
    ])
    selected = pm.select("code")
    assert selected is None  # caller must surface 400/503 to client
