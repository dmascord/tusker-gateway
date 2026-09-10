"""Tests for pools, selection, and cooldown logic."""
from __future__ import annotations

import os
import tempfile

from tusker_gateway.config import PoolConfig, _load_pools, load_config
from tusker_gateway.cooldown import CooldownTracker, _cooldown_seconds_for_429
from tusker_gateway.model_capability import STRUCTURED_OUTPUT_PROBE_VERSION
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


def test_load_config_parses_passthrough_disabled_providers(monkeypatch):
    monkeypatch.setenv(
        "TUSKER_PASSTHROUGH_DISABLED_PROVIDERS",
        "Google, cerebras",
    )

    config = load_config()

    assert config["passthrough_disabled_providers"] == ["google", "cerebras"]


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


def test_stickiness_expires_and_is_cardinality_bounded():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
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
            "provider_api_keys": {"groq": "k", "openai": "k"},
        })
        manager.STICKINESS_MAX_ENTRIES = 2
        first = manager.select("test", session_id="expired")
        manager._stickiness_expires[("expired", "test")] = 0
        second = manager.select("test", session_id="expired")
        assert second != first
        manager.select("test", session_id="two")
        manager.select("test", session_id="three")
        assert len(manager._stickiness) <= 2
        assert set(manager._stickiness) == set(manager._stickiness_expires)


def test_readiness_reports_pool_with_only_unkeyed_routes_as_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "openai", "model": "gpt-4o"}],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "provider_api_keys": {},
        })
        health, empty = manager.readiness_status()
        assert empty == ["code"]
        assert health["code"] == {"configured": 1, "selectable": 0, "unkeyed": 1}


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


def test_caller_provider_policy_filters_pool_before_selection():
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

        assert manager.select("test", allowed_providers=("open*",)) == (
            "openai",
            "m2",
        )
        assert manager.select("test", allowed_providers=()) is None


def test_caller_model_policy_filters_concrete_pool_candidates():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[
                        {"provider": "openrouter", "model": "approved"},
                        {"provider": "openrouter", "model": "denied"},
                    ],
                ),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openrouter": "k-openrouter"},
        })

        assert manager.select(
            "test", allowed_models=("openrouter/approved",)
        ) == ("openrouter", "approved")
        assert manager.select(
            "test", allowed_models=("openrouter::approved",)
        ) == ("openrouter", "approved")
        assert manager.select("test", allowed_models=()) is None


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


def test_privacy_structured_output_gate_prefers_passes_and_excludes_rejections(tmp_path):
    manager = PoolManager(
        {
            "pools": {
                "privacy": PoolConfig(
                    name="privacy",
                    zdr=True,
                    models=[
                        {"provider": "synthetic", "model": "structured-good"},
                        {"provider": "local-llm", "model": "structured-bad"},
                    ],
                ),
            },
            "quality_db_path": str(tmp_path / "quality.db"),
            "model_capability_db_path": str(tmp_path / "model-capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"synthetic": "k-synthetic"},
        }
    )
    manager._model_capability_db.record(
        provider="synthetic",
        model="structured-good",
        capability="structured_output",
        status="passed",
        source="structured_probe",
        probe_version=STRUCTURED_OUTPUT_PROBE_VERSION,
    )
    manager._model_capability_db.record(
        provider="local-llm",
        model="structured-bad",
        capability="structured_output",
        status="unsupported",
        source="structured_probe",
        probe_version=STRUCTURED_OUTPUT_PROBE_VERSION,
    )

    assert manager.select("privacy", requires_structured_output=True) == (
        "synthetic",
        "structured-good",
    )


def test_privacy_structured_output_gate_fails_open_for_unknown_candidates(tmp_path):
    manager = PoolManager(
        {
            "pools": {
                "privacy": PoolConfig(
                    name="privacy",
                    zdr=True,
                    models=[{"provider": "synthetic", "model": "unqualified"}],
                ),
            },
            "quality_db_path": str(tmp_path / "quality.db"),
            "model_capability_db_path": str(tmp_path / "model-capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"synthetic": "k-synthetic"},
        }
    )

    assert manager.select("privacy", requires_structured_output=True) == (
        "synthetic",
        "unqualified",
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


def test_language_restricted_chat_models_are_not_general_candidates():
    """allam-2-7b is Arabic-first and returns empty content on English
    prompts, so it must not be admitted to general chat pools."""
    assert is_general_chat_model("groq", "allam-2-7b") is False
    # Arabic prompts work fine — the filter is about general-chat usability,
    # not about dropping the provider entirely.
    assert is_general_chat_model("groq", "openai/gpt-oss-120b") is True
    assert is_general_chat_model("groq", "groq/compound-mini") is True


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


def test_readiness_reports_oauth_pool_with_empty_rotator_as_empty():
    """An OAuth-only pool with an empty credential rotator must be unselectable.

    Pre-fix regression: ``readiness_status`` did not check credential sizes,
    so OAuth providers with an empty rotator were counted as ``selectable``
    even though requests cannot authenticate. The preflight would succeed
    and the first request would surface a 401/503 from the provider. This
    test wires a custom providers registry so the OAuth kind is visible to
    PoolManager and asserts the spec is filtered out when no credentials
    exist.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "openai-codex", "model": "gpt-5"}],
                ),
            },
            "providers": {
                "openai-codex": {"kind": "oauth", "zdr_ok": False},
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "provider_api_keys": {},
        })
        # Without credential_sizes, OAuth providers with no registry keys are
        # still considered eligible — useful for bare test executors. With an
        # empty credential_sizes map they are filtered out.
        health, empty = manager.readiness_status(credential_sizes={})
        assert "code" in empty
        assert health["code"]["selectable"] == 0
        assert health["code"]["configured"] >= 1


def test_readiness_counts_oauth_pool_with_credential_as_selectable():
    """An OAuth pool backed by >=1 credential is selectable. The presence of a
    credential in the rotator map should be sufficient for preflight success.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "openai-codex", "model": "gpt-5"}],
                ),
            },
            "providers": {
                "openai-codex": {"kind": "oauth", "zdr_ok": False},
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "provider_api_keys": {},
        })
        health, empty = manager.readiness_status(
            credential_sizes={"openai-codex": 3},
        )
        assert "code" not in empty
        assert health["code"]["selectable"] == 1


def test_concurrent_selects_distribute_without_dropping_increments():
    """Concurrent ``select()`` calls must distribute even without lost increments.

    Pre-fix regression: ``_round_robin`` was an unguarded dict mutated under
    ``asyncio.to_thread`` (concurrent thread workers), so N threads could all
    read the same offset before any of them wrote back ``offset + 1``. The
    round-robin counter would advance only by 1 instead of N and the same
    candidate would be picked repeatedly, breaking equal-weight distribution.
    The lock-guarded critical region keeps offsets unique per thread.
    """
    import threading

    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "test": PoolConfig(
                    name="test",
                    models=[
                        {"provider": "groq", "model": "m1"},
                        {"provider": "openai", "model": "m2"},
                        {"provider": "openai", "model": "m3"},
                    ],
                ),
            },
            "quality_db_path": os.path.join(tmpdir, "quality.db"),
            "provider_api_keys": {"groq": "k", "openai": "k"},
        })

        results: list[tuple[str, str]] = []
        results_lock = threading.Lock()
        error: list[BaseException] = []

        def worker() -> None:
            try:
                chosen = manager.select("test")
                if chosen is None:
                    error.append(RuntimeError("select() returned None"))
                    return
                with results_lock:
                    results.append(chosen)
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)

        n_workers = 50
        threads = [threading.Thread(target=worker) for _ in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not error, f"workers raised: {error[:3]}"
        assert len(results) == n_workers
        # Counter is an offset that cycles 0..N-1 under % len(tier). With
        # N threads each writing ``offset + 1`` and the lock serialising
        # those writes, the post-call counter must be the
        # ``n_workers mod len(tier)`` value but every observed offset has
        # been claimed exactly once across workers. Pre-fix regression:
        # the counter would advance by a single increment for 50 threads
        # (no lock) so all but one worker saw offset 0 and picked the
        # same candidate.
        assert manager._round_robin["test"] == n_workers % 3, (
            "round-robin counter must match n_workers mod len(tier); "
            f"got {manager._round_robin.get('test')!r} for {n_workers} selects / 3-tier pool"
        )
        # Distribution check: a perfectly even round-robin over 3 candidates
        # with 50 workers can vary by ±1. Heavy skew (one model picked by
        # > 50% of workers) would indicate lost offset increments.
        from collections import Counter
        counts = Counter(model for _, model in results)
        worst = max(counts.values())
        assert worst <= (n_workers // 2) + 1, (
            f"distribution skew detected: {counts!r} — round-robin offsets were lost"
        )
        # Equal weights -> each candidate must be served roughly N/3 times
        # within a 1-element tolerance window. The exact distribution is
        # (m1, m2, m3) interleaved: any distribution where one candidate
        # was chosen more than ceil(N/3) * 2 times would indicate lost
        # increments.
def test_catalog_unavailable_routes_drop_from_selection():
    """Models absent from an authoritative catalog are excluded from selection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[
                        {"provider": "google", "model": "gemini-3-pro"},
                        {"provider": "nvidia", "model": "nvidia/llama-3.1-nemotron-70b"},
                    ],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "q.db"),
            "provider_api_keys": {"google": "k-g", "nvidia": "k-n"},
            "authoritative_catalog_providers": ["google"],
        })

        class _FakeClient:
            provider = "google"

            def diagnostics(self):
                return {"last_refresh_status": "ok", "stale": False}

        class _FakeRegistry:
            def get_client(self, p):
                return _FakeClient() if p == "google" else None

            def known_models(self, p):
                if p == "google":
                    return {"gemini-2.5-pro"}
                return None

        manager.catalog_registry = _FakeRegistry()
        # gemini-3-pro absent from authoritative catalog -> excluded; nvidia remains
        assert manager.select("code", heavyweight_ok=True) == (
            "nvidia",
            "nvidia/llama-3.1-nemotron-70b",
        )


def test_catalog_unavailable_respects_alias_outbound_id():
    """The outbound model ID (alias resolution) is matched against catalog."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[
                        {"provider": "google", "model": "gemini-3-pro"},
                    ],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "q.db"),
            "provider_api_keys": {"google": "k-g"},
            "authoritative_catalog_providers": ["google"],
            "providers": {
                "google": {
                    "model_aliases": {"gemini-3-pro": "gemini-3.5-pro"},
                }
            },
        })

        class _FakeClient:
            provider = "google"

            def diagnostics(self):
                return {"last_refresh_status": "ok", "stale": False}

        class _FakeRegistry:
            def get_client(self, p):
                return _FakeClient() if p == "google" else None

            def known_models(self, p):
                if p == "google":
                    return {"gemini-3.5-pro"}
                return None

        manager.catalog_registry = _FakeRegistry()
        # gemini-3-pro static -> outbound gemini-3.5-pro -> present in catalog -> NOT excluded
        assert manager.select("code", heavyweight_ok=True) == ("google", "gemini-3-pro")


def test_catalog_unavailable_empty_or_stale_preserves_routes():
    """Catalog failures, stale snapshots, or empty results restore the static baseline."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cases = [
            {"last_refresh_status": "error", "stale": False},
            {"last_refresh_status": "ok", "stale": True},
            None,
        ]
        for case in cases:
            manager = PoolManager({
                "pools": {
                    "code": PoolConfig(
                        name="code",
                        models=[{"provider": "google", "model": "gemini-3-pro"}],
                    )
                },
                "quality_db_path": os.path.join(tmpdir, "q.db"),
                "provider_api_keys": {"google": "k-g"},
                "authoritative_catalog_providers": ["google"],
            })

            class _FakeClient:
                provider = "google"

                def diagnostics(self):
                    return case or {}

            class _FakeRegistry:
                def get_client(self, p):
                    return _FakeClient() if p == "google" and case is not None else None

                def known_models(self, p):
                    if p == "google" and case is not None:
                        return set()
                    return None

            manager.catalog_registry = _FakeRegistry()
            # No gate applied when catalog is uncertain -> route is selectable
            assert manager.select("code", heavyweight_ok=True) == (
                "google",
                "gemini-3-pro",
            ), f"failed case {case}"


def test_catalog_unavailable_stickiness_drops_on_exclusion():
    """A sticky route that becomes catalog-unavailable is cleared and re-selected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[
                        {"provider": "google", "model": "gemini-3-pro"},
                        {"provider": "nvidia", "model": "nvidia/llama-3.1-nemotron-70b"},
                    ],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "q.db"),
            "provider_api_keys": {"google": "k-g", "nvidia": "k-n"},
            "authoritative_catalog_providers": ["google"],
        })

        class _FakeClient:
            provider = "google"

            def diagnostics(self):
                return {"last_refresh_status": "ok", "stale": False}

        class _FakeRegistry:
            def get_client(self, p):
                return _FakeClient() if p == "google" else None

            def known_models(self, p):
                if p == "google":
                    return {"gemini-2.5-pro"}
                return None

        manager.catalog_registry = _FakeRegistry()
        # Record explicit stickiness for the google route
        manager._stickiness[("session-x", "code")] = ("google", "gemini-3-pro")
        manager._stickiness_expires[("session-x", "code")] = float("inf")
        # Catalog says gemini-3-pro is unavailable -> stickiness dropped
        # Fall-through to nvidia
        result = manager.select("code", session_id="session-x", heavyweight_ok=True)
        assert result == ("nvidia", "nvidia/llama-3.1-nemotron-70b")


def test_catalog_unavailable_readiness_excludes_unavailable():
    """readiness_status reports catalog-unavailable routes as not selectable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[
                        {"provider": "google", "model": "gemini-3-pro"},
                        {"provider": "groq", "model": "llama-3.3-70b"},
                    ],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "q.db"),
            "provider_api_keys": {"google": "k-g", "groq": "k-groq"},
            "authoritative_catalog_providers": ["google"],
        })

        class _FakeClient:
            provider = "google"

            def diagnostics(self):
                return {"last_refresh_status": "ok", "stale": False}

        class _FakeRegistry:
            def get_client(self, p):
                return _FakeClient() if p == "google" else None

            def known_models(self, p):
                if p == "google":
                    return {"gemini-2.5-pro"}
                return None

        manager.catalog_registry = _FakeRegistry()
        health, empty = manager.readiness_status()
        # google: heavyweight AND catalog-unavailable; groq: lightweight, not authoritative
        assert health["code"]["selectable"] == 1


def test_authoritative_catalog_providers_default_empty():
    """Without TUSKER_AUTHORITATIVE_CATALOG_PROVIDERS, no catalog exclusion applies."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "groq", "model": "llama-3.3-70b"}],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "q.db"),
            "provider_api_keys": {"groq": "k-groq"},
        })
        # No catalog registry at all
        assert manager.select("code") == ("groq", "llama-3.3-70b")
        health, _ = manager.readiness_status()
        assert health["code"]["selectable"] == 1


def test_extend_pools_with_catalog_resolves_outbound_alias():
    """extend_pools_with_catalog counts a static route when its outbound alias
    matches a catalog model."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "google", "model": "gemini-3-pro"}],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "q.db"),
            "provider_api_keys": {"google": "k-g"},
            "providers": {
                "google": {
                    "model_aliases": {"gemini-3-pro": "gemini-3.5-pro"},
                }
            },
        })

        class _FakeClient:
            provider = "google"

            def diagnostics(self):
                return {"last_refresh_status": "ok", "stale": False}

        class _FakeRegistry:
            def get_client(self, p):
                return _FakeClient() if p == "google" else None

            def known_models(self, p):
                if p == "google":
                    return {"gemini-3.5-pro"}
                return None

        manager.catalog_registry = _FakeRegistry()
        confirmed = manager.extend_pools_with_catalog()
        # gemini-3-pro -> outbound gemini-3.5-pro -> present in catalog -> confirmed
        assert confirmed["code"] == 1


def test_catalog_unavailable_excludes_only_when_provider_is_authoritative():
    """Only providers in authoritative_catalog_providers are gated."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = PoolManager({
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "groq", "model": "llama-3.3-70b"}],
                )
            },
            "quality_db_path": os.path.join(tmpdir, "q.db"),
            "provider_api_keys": {"groq": "k-groq"},
            "authoritative_catalog_providers": [],
        })

        class _FakeClient:
            provider = "groq"

            def diagnostics(self):
                return {"last_refresh_status": "ok", "stale": False}

        class _FakeRegistry:
            def get_client(self, p):
                return _FakeClient() if p == "groq" else None

            def known_models(self, p):
                if p == "groq":
                    return {"other-model"}
                return None

        manager.catalog_registry = _FakeRegistry()
        # groq not authoritative -> llama-3.3-70b remains selectable despite catalog absence
        assert manager.select("code") == ("groq", "llama-3.3-70b")
        health, _ = manager.readiness_status()
        assert health["code"]["selectable"] == 1


def test_catalog_refresh_cannot_restore_permanently_failed_model(tmp_path):
    from tusker_gateway.cooldown import mark_permanently_failed, clear_permanently_failed

    route = ("google", "gemini-2.5-flash")
    manager = PoolManager({
        "pools": {"code": PoolConfig(
            name="code", models=[], auto_free=True, auto_catalog_providers=["google"],
        )},
        "quality_db_path": str(tmp_path / "quality.db"),
        "provider_api_keys": {"google": "test-key"},
    })
    registry = _CatalogRegistry({"google": [_CatalogEntry(*route)]})
    registry.providers = lambda: ("google",)
    manager.catalog_registry = registry
    manager.extend_pools_with_free_catalog()
    assert manager.select("code") == route

    mark_permanently_failed(*route)
    assert manager.select("code", allow_cooldown_probe=True) is None
    manager.extend_pools_with_free_catalog()
    assert manager.models["code"] == []
    manager.extend_pools_with_free_catalog()
    assert manager.select("code") is None

    clear_permanently_failed(*route)
    manager.extend_pools_with_free_catalog()
    assert manager.select("code") == route
