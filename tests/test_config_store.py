"""Safety invariants for the DB-backed configuration store."""
from __future__ import annotations

import pytest

from tusker_gateway.config_store import ConfigConflictError, ConfigStore


def test_primary_code_pool_cannot_be_saved_empty_without_fallback(tmp_path):
    store = ConfigStore(database=tmp_path / "config.db")

    with pytest.raises(ValueError, match="primary code pool"):
        store.upsert_pool({"name": "code", "models": []})


def test_primary_code_pool_can_use_fallback_when_static_models_empty(tmp_path):
    store = ConfigStore(database=tmp_path / "config.db")

    result = store.upsert_pool(
        {"name": "code", "models": [], "fallback_pools": ["premium"]}
    )

    assert result == {"name": "code", "ok": True}


def test_secondary_auto_catalog_pool_may_start_empty(tmp_path):
    store = ConfigStore(database=tmp_path / "config.db")

    result = store.upsert_pool(
        {"name": "privacy", "models": [], "auto_catalog": True}
    )

    assert result == {"name": "privacy", "ok": True}


def test_primary_code_pool_cannot_be_deleted(tmp_path):
    store = ConfigStore(database=tmp_path / "config.db")

    with pytest.raises(ValueError, match="cannot be deleted"):
        store.delete_pool("code")


def test_get_pool_returns_stored_definition(tmp_path):
    store = ConfigStore(database=tmp_path / "config.db")

    store.upsert_pool({
        "name": "privacy",
        "models": [{"provider": "local-llm", "model": "qwen3:4b"}],
        "context_window": 64000,
        "auto_catalog_providers": ["local-llm"],
    })

    result = store.get_pool("privacy")

    assert result["name"] == "privacy"
    assert result["models"] == [{"provider": "local-llm", "model": "qwen3:4b"}]
    assert result["context_window"] == 64000
    assert result["auto_catalog_providers"] == ["local-llm"]


def test_get_pool_raises_keyerror_for_missing(tmp_path):
    store = ConfigStore(database=tmp_path / "config.db")

    with pytest.raises(KeyError, match="pool not found"):
        store.get_pool("missing")


def test_pool_write_rejects_stale_generation_without_mutation(tmp_path):
    store = ConfigStore(database=tmp_path / "config.db")
    store.upsert_pool({
        "name": "code",
        "models": [{"provider": "minimax", "model": "MiniMax-M3"}],
    })
    current_generation = store.generation

    with pytest.raises(ConfigConflictError, match="configuration generation changed"):
        store.upsert_pool(
            {
                "name": "code",
                "models": [{"provider": "zai", "model": "glm-5"}],
            },
            expected_generation=current_generation - 1,
        )

    assert store.generation == current_generation
    assert store.get_pool("code")["models"] == [
        {"provider": "minimax", "model": "MiniMax-M3"}
    ]
