"""Tests for the cleanup refactor: models, auth strategies, persistent cooldown."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from tusker_gateway.cooldown import CooldownTracker, global_tracker
from tusker_gateway.models import Credential, ProviderConfig
from tusker_gateway.persistent_cooldown import PersistentCooldownStore


def test_credential_from_raw_hermes_format():
    """Hermes-format credentials map to the typed Credential model."""
    raw = {
        "access_token": "abc",
        "refresh_token": "rt-1",
        "expires_at_ms": 1_700_000_000_000,
        "label": "damien.01",
        "auth_type": "oauth",
        "provider": "openai-codex",
    }
    cred = Credential.from_raw(raw)
    assert cred.access_token == "abc"
    assert cred.refresh_token == "rt-1"
    assert cred.expires_at_ms == 1_700_000_000_000
    assert cred.provider == "openai-codex"
    assert cred.label == "damien.01"


def test_credential_from_raw_legacy_format():
    """Legacy `token`/`expires_at` fields are normalized into Hermes shape."""
    raw = {"token": "legacy-tok", "expires_at": 1_700_000_000.0}
    cred = Credential.from_raw(raw)
    assert cred.access_token == "legacy-tok"
    assert cred.expires_at_ms == 1_700_000_000_000


def test_credential_to_raw_preserves_hydra_metadata():
    """Round-trip a credential; non-auth metadata is preserved."""
    raw = {
        "access_token": "tok",
        "label": "user1",
        "auth_type": "oauth",
        "priority": 0,
        "request_count": 5,
        "provider": "openai-codex",
    }
    cred = Credential.from_raw(raw)
    out = cred.to_raw()
    assert out["request_count"] == 5
    assert out["priority"] == 0
    assert out["label"] == "user1"


def test_provider_config_roundtrip():
    """ProviderConfig round-trips through raw dict shape."""
    raw = {
        "base_url": "https://example.com",
        "chat_path": "/v1/chat",
        "auth_type": "bearer",
    }
    cfg = ProviderConfig.from_raw(raw)
    assert cfg.base_url == "https://example.com"
    assert cfg.auth_type == "bearer"
    out = cfg.to_raw()
    assert out == raw


def test_provider_config_optional_model_header():
    """model_header is omitted from raw output when not set."""
    cfg = ProviderConfig(base_url="x", chat_path="y", auth_type="oauth")
    assert "model_header" not in cfg.to_raw()


def test_persistent_cooldown_record_and_is_active(tmp_path: Path):
    """Recorded cooldowns are queryable as active in storage."""
    db = tmp_path / "cooldowns.db"
    store = PersistentCooldownStore(db_path=db)
    store.record("openai-codex", "gpt-5.6-luna", 60.0)
    assert store.is_active("openai-codex", "gpt-5.6-luna")


def test_persistent_cooldown_purge_expired(tmp_path: Path):
    """Expired rows are removed by purge_expired."""
    db = tmp_path / "cooldowns.db"
    store = PersistentCooldownStore(db_path=db)
    # Use a tiny duration then sleep
    store.record("groq", "llama", 0.1)
    time.sleep(0.2)
    deleted = store.purge_expired()
    assert deleted == 1
    assert not store.is_active("groq", "llama")


def test_persistent_cooldown_hydrate_loads_active(tmp_path: Path):
    """hydrate() loads active cooldowns into an in-memory tracker."""
    db = tmp_path / "cooldowns.db"
    store = PersistentCooldownStore(db_path=db)
    store.record("openrouter", "m1", 30.0)
    tracker = CooldownTracker()
    loaded = store.hydrate(tracker)
    assert loaded == 1
    assert tracker.is_cooldown("openrouter", "m1")


def test_persistent_cooldown_status(tmp_path: Path):
    """status() returns a summary dict with at least one entry."""
    db = tmp_path / "cooldowns.db"
    store = PersistentCooldownStore(db_path=db)
    store.record("zai", "g1", 60.0)
    s = store.status()
    assert s["active_count"] == 1
    assert s["entries"][0]["provider"] == "zai"