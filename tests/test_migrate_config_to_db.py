"""Tests for migrate_config_to_db --update semantics.

Without ``--update`` existing rows must be skipped (idempotent no-op);
with ``--update`` existing rows must be replaced with the env-derived
values so env/yaml changes propagate into the DB store.
"""
from __future__ import annotations

import json

import pytest

from tusker_gateway.config import PoolConfig, ProviderConfig
from tusker_gateway.config_store import ConfigStore
from tusker_gateway.tools.migrate_config_to_db import _section_counts


@pytest.fixture
def store(tmp_path):
    return ConfigStore(database=tmp_path / "config.db")


def _config(providers=None, pools=None):
    return {
        "providers": providers or {},
        "provider_api_keys": {},
        "pools": pools or {},
        "credential_pools": {},
    }


def _run(store, config, *, update=False, dry_run=False, allow_credential_overwrite=False):
    results = _section_counts(
        store,
        config,
        dry_run=dry_run,
        update=update,
        allow_credential_overwrite=allow_credential_overwrite,
    )
    return {r[0]: r for r in results}


def test_default_skips_existing_pool(store):
    store.upsert_pool({
        "name": "privacy",
        "models": [{"provider": "local-llm", "model": "qwen3:4b"}],
    })

    config = _config(pools={
        "privacy": PoolConfig(
            name="privacy",
            models=[{"provider": "apim", "model": "gpt-5.6-luna"}],
            context_window=64000,
        ),
    })

    counts = _run(store, config)
    _, ins, upd, skip, err = counts["pools"]
    assert (ins, upd, skip, err) == (0, 0, 1, 0)
    assert store.get_pool("privacy")["models"] == [
        {"provider": "local-llm", "model": "qwen3:4b"}
    ]


def test_update_replaces_existing_pool(store):
    store.upsert_pool({
        "name": "privacy",
        "models": [{"provider": "local-llm", "model": "qwen3:4b"}],
    })

    config = _config(pools={
        "privacy": PoolConfig(
            name="privacy",
            models=[{"provider": "apim", "model": "gpt-5.6-luna"}],
            context_window=64000,
        ),
    })

    counts = _run(store, config, update=True)
    _, ins, upd, skip, err = counts["pools"]
    assert (ins, upd, skip, err) == (0, 1, 0, 0)
    assert store.get_pool("privacy")["models"] == [
        {"provider": "apim", "model": "gpt-5.6-luna"}
    ]


def test_update_replaces_existing_provider(store):
    store.upsert_provider({
        "name": "alibaba",
        "base_url": "https://old.example.com",
        "chat_path": "/v1/chat/completions",
        "auth_env": None,
    })

    config = _config(providers={
        "alibaba": ProviderConfig(
            "alibaba",
            "bearer",
            "https://new.example.com",
            "/v1/chat/completions",
            auth_env="ALIBABA_API_KEY",
        ),
    })

    counts = _run(store, config, update=True)
    _, ins, upd, skip, err = counts["providers"]
    assert (ins, upd, skip, err) == (0, 1, 0, 0)
    stored = store.snapshot()["providers"]["alibaba"]
    assert stored["base_url"] == "https://new.example.com"
    assert stored["auth_env"] == "ALIBABA_API_KEY"


def test_default_skips_existing_provider(store):
    store.upsert_provider({
        "name": "alibaba",
        "base_url": "https://old.example.com",
        "chat_path": "/v1/chat/completions",
        "auth_env": None,
    })

    config = _config(providers={
        "alibaba": ProviderConfig(
            "alibaba",
            "bearer",
            "https://new.example.com",
            "/v1/chat/completions",
            auth_env="ALIBABA_API_KEY",
        ),
    })

    counts = _run(store, config)
    _, ins, upd, skip, err = counts["providers"]
    assert (ins, upd, skip, err) == (0, 0, 1, 0)
    stored = store.snapshot()["providers"]["alibaba"]
    assert stored["base_url"] == "https://old.example.com"


def test_new_rows_still_count_as_inserted_with_update_flag(store):
    config = _config(pools={
        "swarm": PoolConfig(
            name="swarm",
            models=[{"provider": "xiaomi", "model": "mimo-v2.5"}],
        ),
    })

    counts = _run(store, config, update=True)
    _, ins, upd, skip, err = counts["pools"]
    assert (ins, upd, skip, err) == (1, 0, 0, 0)
    assert store.get_pool("swarm")["models"] == [
        {"provider": "xiaomi", "model": "mimo-v2.5"}
    ]


def _credential_config(pools):
    config = _config()
    config["credential_pools"] = pools
    return config


def _decrypt_pool(store):
    with store._conn as conn:  # noqa: SLF001
        cursor = conn.execute(
            "SELECT credentials FROM tusker_config_oauth_credentials WHERE provider = ?",
            ("openai-codex",),
        )
        row = cursor.fetchone()
        return json.loads(store._decrypt(conn, row[0]))  # noqa: SLF001


def test_new_credential_pool_inserted(store):
    config = _credential_config({"openai-codex": [{"refresh_token": "rt-1"}]})

    counts = _run(store, config, update=True)
    _, ins, upd, skip, err = counts["credential_pools"]
    assert (ins, upd, skip, err) == (1, 0, 0, 0)
    assert _decrypt_pool(store) == [{"refresh_token": "rt-1"}]


def _seed_codex_pool(store, creds):
    store._ensure_db()  # noqa: SLF001 - create tusker_config_oauth_credentials
    with store._conn as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO tusker_config_oauth_credentials (provider, credentials) "
            "VALUES (?, ?)",
            ("openai-codex", store._encrypt(conn, json.dumps(creds))),  # noqa: SLF001
        )
        conn.commit()


def test_existing_credential_pool_survives_update_flag(store):
    """Regression: --update must never clobber gateway-owned OAuth pools.

    The env-derived pool in this case is the stale auth.json content; the
    DB row is the enrolled credential.
    """
    _seed_codex_pool(store, [{"refresh_token": "rt-enrolled"}])
    config = _credential_config({"openai-codex": [{"token": "gho_stale"}]})

    counts = _run(store, config, update=True)

    _, ins, upd, skip, err = counts["credential_pools"]
    assert (ins, upd, skip, err) == (0, 0, 1, 0)
    assert _decrypt_pool(store) == [{"refresh_token": "rt-enrolled"}]


def test_credential_overwrite_without_update_still_skips(store):
    """The opt-in flag alone is inert — propagation is still gated on --update."""
    _seed_codex_pool(store, [{"refresh_token": "rt-enrolled"}])
    config = _credential_config({"openai-codex": [{"refresh_token": "rt-new"}]})

    counts = _run(store, config, update=False, allow_credential_overwrite=True)

    _, ins, upd, skip, err = counts["credential_pools"]
    assert (ins, upd, skip, err) == (0, 0, 1, 0)
    assert _decrypt_pool(store) == [{"refresh_token": "rt-enrolled"}]


def test_credential_pool_replaced_with_explicit_optin(store):
    _seed_codex_pool(store, [{"refresh_token": "rt-old"}])
    config = _credential_config({"openai-codex": [{"refresh_token": "rt-new"}]})

    counts = _run(store, config, update=True, allow_credential_overwrite=True)

    _, ins, upd, skip, err = counts["credential_pools"]
    assert (ins, upd, skip, err) == (0, 1, 0, 0)
    assert _decrypt_pool(store) == [{"refresh_token": "rt-new"}]
