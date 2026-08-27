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


def test_unrated_model_does_not_outrank_measured_model():
    """New catalog entries must not outrank a proven healthy candidate."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[
                        {"provider": "openai", "model": "new-model"},
                        {"provider": "groq", "model": "proven-model"},
                    ],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
            "provider_api_keys": {"groq": "k-groq", "openai": "k-openai"},
        }
        mgr = PoolManager(config)
        mgr._quality.record("groq", "proven-model", True, 500.0)
        assert mgr.select("test") == ("groq", "proven-model")


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


class _CatalogEntry:
    def __init__(
        self,
        provider: str,
        model: str,
        *,
        cost_input: float | None = None,
        cost_output: float | None = None,
        input_modalities: frozenset[str] | None = None,
        raw: dict | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.cost_input = cost_input
        self.cost_output = cost_output
        self.input_modalities = input_modalities
        self.raw = raw or {}


class _CatalogRegistry:
    def __init__(self, entries: dict[str, list[_CatalogEntry]]) -> None:
        self._entries = entries

    def entries_for(self, provider: str) -> list[_CatalogEntry] | None:
        return self._entries.get(provider)


def _xiaomi_pool_manager(tmpdir: str, pools: dict[str, PoolConfig]) -> PoolManager:
    return PoolManager({
        "pools": pools,
        "quality_db_path": os.path.join(tmpdir, "quality.db"),
        "excluded_providers": [],
        "provider_api_keys": {"xiaomi": "k-xiaomi"},
    })


def test_selection_filters_known_modalities_and_invalidates_stickiness():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = _xiaomi_pool_manager(tmpdir, {
            "code": PoolConfig(name="code", models=[
                {
                    "provider": "xiaomi",
                    "model": "mimo-v2.5-pro",
                    "input_modalities": ["text"],
                },
                {
                    "provider": "xiaomi",
                    "model": "mimo-v2.5",
                    "input_modalities": ["text", "image"],
                },
            ]),
        })

        assert manager.select("code", session_id="sticky") == (
            "xiaomi", "mimo-v2.5-pro",
        )
        assert manager.select(
            "code",
            excluded={("xiaomi", "mimo-v2.5-pro")},
            required_input_modalities={"text"},
        ) == ("xiaomi", "mimo-v2.5")
        assert manager.select(
            "code",
            session_id="sticky",
            required_input_modalities={"text", "image"},
        ) == ("xiaomi", "mimo-v2.5")
        assert manager._stickiness[("sticky", "code")] == (
            "xiaomi", "mimo-v2.5",
        )


def test_unknown_modalities_remain_eligible_for_existing_providers():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "test": PoolConfig(name="test", models=[
                    {"provider": "local-llm", "model": "legacy"},
                ]),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
        })

        assert manager.select(
            "test", required_input_modalities={"text", "image"},
        ) == ("local-llm", "legacy")


def test_selection_filters_catalog_models_without_tools_or_images():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(name="code", models=[
                    {"provider": "openrouter", "model": "text-only"},
                    {"provider": "openrouter", "model": "image-no-tools"},
                    {"provider": "openrouter", "model": "tool-image"},
                ]),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openrouter": "k-openrouter"},
        })
        manager.catalog_registry = _CatalogRegistry({
            "openrouter": [
                _CatalogEntry(
                    "openrouter", "text-only",
                    raw={
                        "architecture": {"input_modalities": ["text"]},
                        "supported_parameters": ["max_tokens"],
                    },
                ),
                _CatalogEntry(
                    "openrouter", "image-no-tools",
                    raw={
                        "architecture": {"input_modalities": ["text", "image"]},
                        "supported_parameters": ["max_tokens"],
                    },
                ),
                _CatalogEntry(
                    "openrouter", "tool-image",
                    raw={
                        "architecture": {"input_modalities": ["text", "image"]},
                        "supported_parameters": ["max_tokens", "tools"],
                    },
                ),
            ],
        })

        assert manager.select(
            "code",
            required_input_modalities={"image"},
            requires_tools=True,
        ) == ("openrouter", "tool-image")


def test_xiaomi_catalog_auto_adds_only_nonheavy_chat_models_to_code():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = _xiaomi_pool_manager(tmpdir, {
            "code": PoolConfig(name="code", models=[], auto_free=True),
            "privacy": PoolConfig(
                name="privacy", models=[], zdr=True, auto_free=True,
            ),
            "premium": PoolConfig(name="premium", models=[], auto_free=True),
        })
        manager.catalog_registry = _CatalogRegistry({
            "xiaomi": [
                _CatalogEntry(
                    "xiaomi",
                    "mimo-v2.5",
                    cost_input=0.14,
                    cost_output=0.28,
                    input_modalities=frozenset({"text", "image"}),
                ),
                _CatalogEntry(
                    "xiaomi",
                    "mimo-v2.5-pro",
                    cost_input=0.435,
                    cost_output=0.87,
                    input_modalities=frozenset({"text"}),
                ),
                _CatalogEntry(
                    "xiaomi",
                    "expensive-chat",
                    cost_input=1.0,
                    cost_output=0.5,
                    input_modalities=frozenset({"text"}),
                ),
            ],
        })

        manager.extend_pools_with_free_catalog()

        code = {(spec.provider, spec.model): spec for spec in manager.models["code"]}
        assert set(code) == {
            ("xiaomi", "mimo-v2.5"),
            ("xiaomi", "mimo-v2.5-pro"),
        }
        assert code[("xiaomi", "mimo-v2.5")].input_modalities == frozenset({
            "text", "image",
        })
        assert code[("xiaomi", "mimo-v2.5-pro")].input_modalities == frozenset({
            "text",
        })
        assert manager.models["privacy"] == []
        assert manager.models["premium"] == []


def test_static_xiaomi_privacy_entry_remains_operator_curated():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = _xiaomi_pool_manager(tmpdir, {
            "privacy": PoolConfig(
                name="privacy",
                models=[{
                    "provider": "xiaomi",
                    "model": "mimo-v2.5-pro",
                    "input_modalities": ["text"],
                }],
                zdr=True,
                auto_free=True,
            ),
        })
        manager.catalog_registry = _CatalogRegistry({
            "xiaomi": [
                _CatalogEntry(
                    "xiaomi",
                    "mimo-v2.5",
                    cost_input=0.14,
                    cost_output=0.28,
                    input_modalities=frozenset({"text", "image"}),
                ),
            ],
        })

        manager.extend_pools_with_free_catalog()

        assert [(spec.provider, spec.model) for spec in manager.models["privacy"]] == [
            ("xiaomi", "mimo-v2.5-pro"),
        ]
