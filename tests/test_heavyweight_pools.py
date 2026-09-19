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


def test_ollama_cloud_pricing_overlay_filters_heavyweight_from_code_pool():
    """Regression: ollama-cloud auto-catalog entries priced above the
    $1/M-in or $3/M-out thresholds must be filtered from the cheap code
    pool. kimi-k3 is heavy by slug override; glm-5.1/5.2/5.3, kimi-k2.6/2.7-code
    are heavy by pricing; glm-5.3-flash, minimax-m3 stay light."""
    heavy = {"kimi-k3", "glm-5.1", "glm-5.2", "glm-5.3", "kimi-k2.6", "kimi-k2.7-code"}
    light = {"glm-5.3-flash", "minimax-m3"}
    from tusker_gateway.catalog import (
        CatalogEntry,
        CatalogRegistry,
        ProviderModelsCatalog,
    )

    cfg = {
        "pools": {
            "code": PoolConfig(name="code", models=[], auto_catalog=True,
                               auto_catalog_providers=["ollama-cloud"]),
        },
        "excluded_providers": [],
        "provider_api_keys": {"ollama-cloud": "k"},
        "quality_db_path": tempfile.mktemp(suffix=".db"),
    }
    pm = PoolManager(cfg)
    registry = CatalogRegistry()
    ollama = ProviderModelsCatalog(
        provider="ollama-cloud",
        endpoint="https://ollama.com/v1/models",
    )
    # Entries with overlay pricing applied (fetch() behaviour covered in
    # test_catalog.py); the pool gate must act on the costs.
    ollama._entries = [
        CatalogEntry(provider="ollama-cloud", model="kimi-k3",
                     cost_input=3.00, cost_output=15.00),
        CatalogEntry(provider="ollama-cloud", model="glm-5.1",
                     cost_input=1.00, cost_output=3.20),
        CatalogEntry(provider="ollama-cloud", model="glm-5.2",
                     cost_input=1.40, cost_output=4.40),
        CatalogEntry(provider="ollama-cloud", model="glm-5.3",
                     cost_input=1.40, cost_output=4.40),
        CatalogEntry(provider="ollama-cloud", model="glm-5.3-flash",
                     cost_input=0.15, cost_output=0.50),
        CatalogEntry(provider="ollama-cloud", model="kimi-k2.6",
                     cost_input=0.95, cost_output=4.00),
        CatalogEntry(provider="ollama-cloud", model="kimi-k2.7-code",
                     cost_input=0.95, cost_output=4.00),
        CatalogEntry(provider="ollama-cloud", model="minimax-m3",
                     cost_input=0.60, cost_output=2.40),
        CatalogEntry(provider="ollama-cloud", model="nemotron-3-ultra",
                     cost_input=0.10, cost_output=3.00),
        CatalogEntry(provider="ollama-cloud", model="qwen3.5:397b",
                     cost_input=0.60, cost_output=3.60),
    ]
    registry.register("ollama-cloud", ollama)
    pm.catalog_registry = registry
    pm.extend_pools_with_auto_catalog()

    pool_by_pair = {
        (m["provider"], m["model"]): m
        for m in pm.pools["code"].models
    }
    heavy = {"kimi-k3", "glm-5.1", "glm-5.2", "glm-5.3", "kimi-k2.6", "kimi-k2.7-code",
             "nemotron-3-ultra", "qwen3.5:397b"}
    light = {"glm-5.3-flash", "minimax-m3"}
    for m in heavy | light:
        assert pool_by_pair[("ollama-cloud", m)]["heavyweight"] is (m in heavy)
    # select() drops heavies; a light candidate serves the request.
    selected = pm.select("code")
    assert selected is not None
    assert selected[0] == "ollama-cloud"
    assert selected[1] in light


def test_premium_heavyweight_only_adopts_heavy_catalog_entries():
    """Regression: a premium-tier pool with heavyweight_only=True opt-in
    auto-adopts only heavyweight catalog entries (docs/solution.md:
    "heavyweight or premium candidates"). Static entries — including
    normally-light ones like syn:large:text — are unaffected."""
    from tusker_gateway.catalog import (
        CatalogEntry,
        CatalogRegistry,
        ProviderModelsCatalog,
    )

    cfg = {
        "pools": {
            "premium": PoolConfig(
                name="premium",
                models=[
                    {"provider": "openai-codex", "model": "gpt-5.6-sol"},
                    {"provider": "synthetic", "model": "syn:large:text"},
                ],
                auto_catalog=True,
                auto_catalog_providers=["ollama-cloud"],
                heavyweight_only=True,
            ),
        },
        "provider_api_keys": {"ollama-cloud": "k", "synthetic": "s"},
        "quality_db_path": tempfile.mktemp(suffix=".db"),
    }
    pm = PoolManager(cfg)
    registry = CatalogRegistry()
    ollama = ProviderModelsCatalog(
        provider="ollama-cloud",
        endpoint="https://ollama.com/v1/models",
    )
    ollama._entries = [
        CatalogEntry(provider="ollama-cloud", model="kimi-k3",
                     cost_input=3.00, cost_output=15.00),
        CatalogEntry(provider="ollama-cloud", model="glm-5.3",
                     cost_input=1.40, cost_output=4.40),
        CatalogEntry(provider="ollama-cloud", model="glm-5.3-flash",
                     cost_input=0.15, cost_output=0.50),
        CatalogEntry(provider="ollama-cloud", model="kimi-k2.6",
                     cost_input=0.95, cost_output=4.00),
    ]
    registry.register("ollama-cloud", ollama)
    pm.catalog_registry = registry
    pm.extend_pools_with_auto_catalog()

    premium = {(s.provider, s.model): s for s in pm.models["premium"]}
    assert ("openai-codex", "gpt-5.6-sol") in premium  # static kept
    assert ("synthetic", "syn:large:text") in premium  # static kept (light)
    assert ("ollama-cloud", "kimi-k3") in premium      # heavy adopted
    assert ("ollama-cloud", "glm-5.3") in premium      # heavy adopted
    assert ("ollama-cloud", "glm-5.3-flash") not in premium  # light skipped
    assert ("ollama-cloud", "kimi-k2.6") in premium      # heavy by pricing ($4/M out)
    assert pm.select("premium") is not None



def test_premium_heavyweight_only_requires_auto_catalog():
    """Regression from the live rollout: without auto_catalog=True the pool is
    skipped entirely by extend_pools_with_auto_catalog — heavyweight_only
    alone adopts nothing."""
    from tusker_gateway.catalog import (
        CatalogEntry,
        CatalogRegistry,
        ProviderModelsCatalog,
    )

    cfg = {
        "pools": {
            "premium": PoolConfig(
                name="premium",
                models=[{"provider": "openai-codex", "model": "gpt-5.6-sol"}],
                auto_catalog=False,
                auto_catalog_providers=["ollama-cloud"],
                heavyweight_only=True,
            ),
        },
        "provider_api_keys": {"ollama-cloud": "k"},
        "quality_db_path": tempfile.mktemp(suffix=".db"),
    }
    pm = PoolManager(cfg)
    registry = CatalogRegistry()
    ollama = ProviderModelsCatalog(
        provider="ollama-cloud",
        endpoint="https://ollama.com/v1/models",
    )
    ollama._entries = [
        CatalogEntry(provider="ollama-cloud", model="kimi-k3",
                     cost_input=3.00, cost_output=15.00),
    ]
    registry.register("ollama-cloud", ollama)
    pm.catalog_registry = registry
    pm.extend_pools_with_auto_catalog()

    premium = {(s.provider, s.model) for s in pm.models["premium"]}
    assert ("ollama-cloud", "kimi-k3") not in premium
    assert premium == {("openai-codex", "gpt-5.6-sol")}
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
