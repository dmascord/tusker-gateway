"""Safety invariants for the DB-backed configuration store."""
from __future__ import annotations

import sqlite3

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


def _cred(refresh: str, **extra: object) -> dict:
    cred = {
        "refresh_token": refresh,
        "access_token": f"access-{refresh}",
        "expires_at_ms": 1_700_000_000_000,
    }
    cred.update(extra)
    return cred


def test_load_oauth_credentials_returns_none_for_unknown_provider(tmp_path):
    """None means "no row" and must stay distinct from an empty list."""
    store = ConfigStore(database=tmp_path / "config.db")

    assert store.load_oauth_credentials("openai-codex") is None


def test_persist_credentials_seeds_row_when_provider_never_stored(tmp_path):
    """A pool that was never written through the admin API must not lose rotations.

    Before seeding, the CAS lookup returned False on a missing row and the
    refreshed token was silently dropped, leaving the DB stale forever.
    """
    store = ConfigStore(database=tmp_path / "config.db")
    expected = _cred("refresh-1")
    replacement = _cred("refresh-2")

    assert store.persist_credentials("openai-codex", expected, replacement) is True
    assert store.load_oauth_credentials("openai-codex") == [replacement]


def test_persist_credentials_replaces_matching_entry_only(tmp_path):
    """CAS replaces the matching credential and leaves siblings intact."""
    store = ConfigStore(database=tmp_path / "config.db")
    other = _cred("refresh-other")
    store.persist_credentials("openai-codex", _cred("unused"), other)
    store.persist_credentials("openai-codex", other, other)

    expected = _cred("refresh-1")
    store.persist_credentials("openai-codex", other, other)
    store.persist_credentials(
        "openai-codex", _cred("never-there"), _cred("never-there-2")
    )
    assert store.load_oauth_credentials("openai-codex") == [other]

    store.persist_credentials("openai-codex", other, expected)
    store.persist_credentials("openai-codex", expected, _cred("refresh-3"))

    stored = store.load_oauth_credentials("openai-codex")
    assert stored == [_cred("refresh-3")]


def test_persist_credentials_returns_false_on_cas_conflict(tmp_path):
    """A concurrent admin replacement makes ``expected`` vanish: no write."""
    store = ConfigStore(database=tmp_path / "config.db")
    live = _cred("live-refresh")
    store.persist_credentials("openai-codex", _cred("seed"), live)

    stale = _cred("stale-refresh")
    assert store.persist_credentials("openai-codex", stale, _cred("nope")) is False
    assert store.load_oauth_credentials("openai-codex") == [live]


def test_provider_embed_path_round_trips_through_store(tmp_path):
    """embed_path is a first-class provider field, not just rerank_path."""
    store = ConfigStore(database=tmp_path / "config.db")
    store.upsert_provider({
        "name": "voyage",
        "base_url": "https://api.voyageai.com",
        "embed_path": "/v1/embeddings",
        "rerank_path": "/v1/rerank",
    })

    assert store.snapshot()["providers"]["voyage"]["embed_path"] == "/v1/embeddings"

    provider = store.runtime_config({})["providers"]["voyage"]
    assert provider.embed_path == "/v1/embeddings"
    assert provider.rerank_path == "/v1/rerank"


def test_embed_path_column_is_added_to_a_legacy_database(tmp_path):
    """An existing deployment's provider table gains the column on startup."""
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE tusker_config_providers ("
            "name TEXT PRIMARY KEY, base_url TEXT NOT NULL, "
            "chat_path TEXT NOT NULL DEFAULT '/v1/chat/completions', "
            "auth_env TEXT, pool_env TEXT, model_header TEXT, api_key_header TEXT, "
            "models_path TEXT, rerank_path TEXT, model_aliases TEXT, "
            "zdr_ok INTEGER NOT NULL DEFAULT 0, "
            "heavyweight INTEGER NOT NULL DEFAULT 0, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )

    store = ConfigStore(database=db_path)
    store.upsert_provider({
        "name": "voyage",
        "base_url": "https://api.voyageai.com",
        "embed_path": "/v1/embeddings",
    })

    assert store.snapshot()["providers"]["voyage"]["embed_path"] == "/v1/embeddings"
