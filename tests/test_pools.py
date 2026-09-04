"""Tests for pools, selection, and cooldown logic."""
from __future__ import annotations

import os
import tempfile

from tusker_gateway.config import PoolConfig, _load_pools, load_config
from tusker_gateway.cooldown import CooldownTracker, _cooldown_seconds_for_429
from tusker_gateway.pools import ModelSpec, PoolManager, is_general_chat_model


def test_default_code_pool_includes_current_provider_routes(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("TUSKER_POOL_") or key == "TUSKER_AUTO_CATALOG_PROVIDERS":
            monkeypatch.delenv(key, raising=False)

    pool = _load_pools()["code"]
    models = pool.models
    routes = {
        (model["provider"], model["model"])
        for model in models
    }

    assert {
        ("groq", "openai/gpt-oss-120b"),
        ("groq", "openai/gpt-oss-20b"),
        ("groq", "qwen/qwen3.6-27b"),
        ("arcee", "trinity-mini"),
    } <= routes
    assert "opencode-go" in pool.auto_catalog_providers


def test_synthetic_is_eligible_for_privacy_pool(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("TUSKER_POOL_") or key == "TUSKER_AUTO_CATALOG_PROVIDERS":
            monkeypatch.delenv(key, raising=False)

    from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY

    assert DEFAULT_PROVIDER_REGISTRY["synthetic"].zdr_ok is True
    routes = {
        (model["provider"], model["model"])
        for model in _load_pools()["privacy"].models
    }
    assert {
        ("synthetic", "syn:large:text"),
        ("synthetic", "syn:small:text"),
        ("synthetic", "syn:large:vision"),
        ("synthetic", "syn:small:vision"),
    } <= routes


def test_business_copilot_is_available_to_privacy_catalog(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("TUSKER_POOL_") or key == "TUSKER_AUTO_CATALOG_PROVIDERS":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TUSKER_COPILOT_BUSINESS", "true")

    pool = _load_pools()["privacy"]

    assert "github-copilot" in pool.auto_catalog_providers


def test_load_config_normalizes_auto_free_provider_exclusions(monkeypatch):
    monkeypatch.setenv("TUSKER_AUTO_FREE_EXCLUDED_PROVIDERS", "NVIDIA, open_router")
    config = load_config()

    assert config["auto_free_excluded_providers"] == ["nvidia", "open-router"]


def test_pool_config_normalizes_auto_catalog_providers():
    pool = PoolConfig(
        name="code",
        models=[],
        auto_free=True,
        auto_catalog_providers="GitHub_Copilot, ZAI",
    )

    assert pool.auto_catalog_providers == ("github-copilot", "zai")


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


def test_equal_weight_candidates_round_robin():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[
                        {"provider": "groq", "model": "m1"},
                        {"provider": "openai", "model": "m2"},
                    ],
                ),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
            "provider_api_keys": {"groq": "k-groq", "openai": "k-openai"},
        })

        selections = [manager.select("test") for _ in range(4)]

    assert selections == [
        ("groq", "m1"),
        ("openai", "m2"),
        ("groq", "m1"),
        ("openai", "m2"),
    ]


def test_verified_modality_evidence_controls_pool_selection():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[{"provider": "groq", "model": "vision-candidate"}],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "model_capability_db_path": os.path.join(tmpdir, "model-capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"groq": "k-groq"},
        }
        manager = PoolManager(config)
        manager._model_capability_db.record(
            provider="groq",
            model="vision-candidate",
            capability="input_image",
            status="unsupported",
            source="modality_probe",
        )
        assert manager.select("test", required_input_modalities={"image"}) is None

        manager._model_capability_db.record(
            provider="groq",
            model="vision-candidate",
            capability="input_image",
            status="passed",
            source="modality_probe",
        )
        assert manager.select("test", required_input_modalities={"image"}) == (
            "groq",
            "vision-candidate",
        )


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


def test_disabled_provider_is_removed_from_pool_candidates(tmp_path):
    manager = PoolManager(
        {
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[
                        {"provider": "arcee", "model": "trinity-mini"},
                        {"provider": "groq", "model": "gpt-oss"},
                    ],
                ),
            },
            "quality_db_path": os.path.join(tmp_path, "quality.db"),
            "provider_api_keys": {"groq": "k-groq"},
            "disabled_providers": ["arcee"],
        }
    )

    assert manager.select("code") == ("groq", "gpt-oss")
    assert manager.status()["code"]["valid_candidates"] == 1


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


def test_cooldown_probe_ignores_model_cooldown_but_keeps_capacity_quarantine():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[{"provider": "openai-codex", "model": "m1"}],
                ),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
        })
        manager._cooldowns.cooldown("openai-codex", "m1", 30)

        assert manager.select("test") is None
        assert manager.select("test", allow_cooldown_probe=True) == (
            "openai-codex",
            "m1",
        )


def test_empty_pool_selection_logs_filter_breakdown(caplog):
    """An exhausted pool must emit a usable diagnostic, not a logging error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[{"provider": "openai-codex", "model": "m1"}],
                ),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
        })
        manager._cooldowns.cooldown("openai-codex", "m1", 30)

        with caplog.at_level("WARNING", logger="tusker_gateway.pools"):
            assert manager.select("test", requires_tools=True) is None

    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "configured=1" in message
    assert "requires_tools=True" in message
    assert "filters=cooldown=1" in message


def test_empty_pool_logs_unkeyed_candidates(caplog):
    """Credential filtering must be visible when it empties a pool."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[{"provider": "groq", "model": "m1"}],
                ),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
            "provider_api_keys": {},
        })

        with caplog.at_level("WARNING", logger="tusker_gateway.pools"):
            assert manager.select("test") is None

    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "configured=1" in message
    assert "usable=0" in message
    assert "unkeyed=1" in message


def test_pool_fallbacks_are_explicit_and_ignore_unknown_or_self_references():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "openai-codex", "model": "code-model"}],
                    fallback_pools=["premium", "missing", "code"],
                ),
                "premium": PoolConfig(
                    name="premium",
                    models=[{"provider": "openai-codex", "model": "premium-model"}],
                ),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
        })

        assert manager.fallback_pools("code") == ("premium",)


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


def test_minimax_m3_can_cover_image_tool_requests():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(name="code", models=[
                    {
                        "provider": "minimax",
                        "model": "MiniMax-M3",
                        "input_modalities": ["text", "image"],
                    },
                    {
                        "provider": "minimax",
                        "model": "MiniMax-M2.7",
                        "input_modalities": ["text"],
                    },
                ]),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
            "provider_api_keys": {"minimax": "k-minimax"},
        })

        assert manager.select(
            "code",
            required_input_modalities={"image"},
            requires_tools=True,
        ) == ("minimax", "MiniMax-M3")


def test_unknown_non_text_modalities_are_not_eligible_without_evidence():
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
            "test", required_input_modalities={"image"},
        ) is None

        manager._model_capability_db.record(
            provider="local-llm",
            model="legacy",
            capability="input_image",
            status="passed",
            source="modality_probe",
        )
        assert manager.select(
            "test", required_input_modalities={"image"},
        ) == ("local-llm", "legacy")


def test_configured_modality_names_are_normalized():
    spec = ModelSpec.from_dict({
        "provider": "openai",
        "model": "vision-model",
        "input_modalities": ["TEXT", "image-input"],
    })

    assert spec.input_modalities == frozenset({"text", "image_input"})


def test_auto_discovered_unknown_non_text_modality_requires_evidence():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(name="code", models=[
                    {
                        "provider": "groq",
                        "model": "catalog-model",
                        "auto_discovered": True,
                    },
                    {
                        "provider": "synthetic",
                        "model": "syn:large:vision",
                        "input_modalities": ["text", "image"],
                    },
                ]),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "model_capability_db_path": os.path.join(tmpdir, "model-capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"groq": "k-groq", "synthetic": "k-synthetic"},
        })

        assert manager.select(
            "code", required_input_modalities={"image"},
        ) == ("synthetic", "syn:large:vision")

        manager._model_capability_db.record(
            provider="groq",
            model="catalog-model",
            capability="input_image",
            status="passed",
            source="modality_probe",
        )
        assert manager.select(
            "code", required_input_modalities={"image"},
        ) == ("groq", "catalog-model")


def test_selection_excludes_special_purpose_and_provider_router_models():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(name="code", models=[
                    {
                        "provider": "openrouter",
                        "model": "nvidia/nemotron-3.5-content-safety:free",
                    },
                    {"provider": "openrouter", "model": "openrouter/free"},
                    {"provider": "openrouter", "model": "openrouter/auto"},
                    {"provider": "openrouter", "model": "openai/gpt-oss-20b:free"},
                ]),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openrouter": "k-openrouter"},
        })

        assert manager.select("code") == (
            "openrouter", "openai/gpt-oss-20b:free",
        )


def test_live_audio_models_are_not_general_chat_candidates():
    """Gemini Live/native-audio IDs require WebSocket, not HTTP chat."""
    assert is_general_chat_model(
        "google", "gemini-2.5-flash-native-audio-latest",
    ) is False
    assert is_general_chat_model(
        "google", "gemini-3.1-flash-live-preview",
    ) is False
    assert is_general_chat_model(
        "google", "gemini-2.5-flash-image",
    ) is False
    assert is_general_chat_model(
        "google", "gemini-2.5-computer-use-preview-10-2025",
    ) is False
    assert is_general_chat_model(
        "google", "deep-research-preview-04-2026",
    ) is False
    assert is_general_chat_model(
        "google", "gemini-2.5-flash-preview-09-2025",
    ) is True
    # A model slug mentioning image is not automatically image generation.
    assert is_general_chat_model("openrouter", "tool-image") is True


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


def test_status_reports_catalog_and_live_modality_evidence():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(name="code", models=[
                    {"provider": "openrouter", "model": "vision-model"},
                ]),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "model_capability_db_path": os.path.join(tmpdir, "model-capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openrouter": "k-openrouter"},
        })
        manager.catalog_registry = _CatalogRegistry({
            "openrouter": [
                _CatalogEntry(
                    "openrouter",
                    "vision-model",
                    raw={
                        "architecture": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                        },
                    },
                ),
            ],
        })
        manager._model_capability_db.record(
            provider="openrouter",
            model="vision-model",
            capability="input_image",
            status="passed",
            source="modality_probe",
        )

        candidate = manager.status()["code"]["candidates"][0]
        assert candidate["input_modalities"] == ["image", "text"]
        assert candidate["output_modalities"] == ["text"]
        assert candidate["model_capabilities"][0]["status"] == "passed"


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
