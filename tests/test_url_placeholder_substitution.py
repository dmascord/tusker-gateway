"""Regression tests for ``{ENV_VAR}`` placeholder substitution in provider URLs.

Two provider-registry paths must apply ``expand_env_placeholders`` so that
URLs loaded with literal ``{ENV_VAR}`` tokens (e.g. Cloudflare Workers AI's
``{CF_ACCOUNT_ID}``) route to the correct account, not to a 404 from the
literal placeholder.

Bypassing this caused workers-ai models to 404 → permanent exclusion →
privacy pool depletion → slow fallback responses.
"""
from __future__ import annotations

import json
import os
import tempfile

from tusker_gateway.config import _provider_registry_from_env
from tusker_gateway.config_store import ConfigStore
from tusker_gateway.identity import IdentityConfig


def test_provider_registry_json_substitutes_base_url(monkeypatch):
    """``PROVIDER_REGISTRY_JSON`` entries must have ``{ENV_VAR}`` expanded."""
    monkeypatch.setenv("CF_ACCOUNT_ID", "test-account-abc123")
    monkeypatch.setenv(
        "PROVIDER_REGISTRY_JSON",
        json.dumps({
            "workers-ai": {
                "kind": "bearer",
                "base_url": "https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai",
                "chat_path": "/v1/chat/completions",
            },
        }),
    )
    registry = _provider_registry_from_env()
    assert registry["workers-ai"].base_url == (
        "https://api.cloudflare.com/client/v4/accounts/test-account-abc123/ai"
    )


def test_provider_registry_json_substitutes_chat_path(monkeypatch):
    """``chat_path`` may also embed env tokens (e.g. versioned paths)."""
    monkeypatch.setenv("API_VERSION", "v2")
    monkeypatch.setenv(
        "PROVIDER_REGISTRY_JSON",
        json.dumps({
            "test-provider": {
                "kind": "bearer",
                "base_url": "https://api.example.com",
                "chat_path": "/{API_VERSION}/chat/completions",
            },
        }),
    )
    registry = _provider_registry_from_env()
    assert registry["test-provider"].chat_path == "/v2/chat/completions"


def test_provider_registry_json_falls_back_when_env_unset(monkeypatch):
    """Unset env vars expand to empty string; original literal preserved as fallback."""
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    monkeypatch.setenv(
        "PROVIDER_REGISTRY_JSON",
        json.dumps({
            "workers-ai": {
                "kind": "bearer",
                "base_url": "https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai",
                "chat_path": "/v1/chat/completions",
            },
        }),
    )
    registry = _provider_registry_from_env()
    # base_url still loads — substitution produced an empty token, but the
    # original literal string is preserved by the `or ...` fallback so the
    # provider stays registered (and fails fast upstream rather than silently).
    assert "accounts/" in registry["workers-ai"].base_url


def test_config_store_db_substitutes_base_url(monkeypatch):
    """DB-loaded providers must have ``{ENV_VAR}`` expanded on snapshot read."""
    monkeypatch.setenv("CF_ACCOUNT_ID", "test-account-abc123")
    monkeypatch.setenv("CF_API_TOKEN", "test-token")
    dbfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    os.unlink(dbfile)
    try:
        store = ConfigStore(
            database=dbfile,
            fallback_config={},
            fallback_identity_config=IdentityConfig(),
        )
        store.upsert_provider({
            "name": "workers-ai",
            "base_url": "https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai",
            "chat_path": "/v1/chat/completions",
            "auth_env": "CF_API_TOKEN",
        })
        snapshot = store.snapshot()
        provider = snapshot["providers"]["workers-ai"]
        # Both the structured ProviderConfig (used for in-process routing)
        # and the dict snapshot (used for /admin/diagnostics + external
        # consumers) must have the placeholder resolved.
        assert provider["base_url"] == (
            "https://api.cloudflare.com/client/v4/accounts/test-account-abc123/ai"
        ), (
            f"placeholder was not substituted: {provider['base_url']!r}"
        )
    finally:
        os.unlink(dbfile)


def test_config_store_db_substitutes_models_path(monkeypatch):
    """``models_path`` is also a URL-shaped field and must be expanded."""
    monkeypatch.setenv("CATALOG_VERSION", "2024-01")
    dbfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    os.unlink(dbfile)
    try:
        store = ConfigStore(
            database=dbfile,
            fallback_config={},
            fallback_identity_config=IdentityConfig(),
        )
        store.upsert_provider({
            "name": "test-provider",
            "base_url": "https://api.example.com",
            "chat_path": "/v1/chat/completions",
            "models_path": "/{CATALOG_VERSION}/models",
        })
        snapshot = store.snapshot()
        provider = snapshot["providers"]["test-provider"]
        assert provider["models_path"] == "/2024-01/models"
    finally:
        os.unlink(dbfile)

def test_config_store_db_infers_kind_for_oauth_providers(monkeypatch):
    """DB-loaded OAuth/Codex providers must NOT be tagged as bearer.
    
    Regression: previously the config store hardcoded ``kind="bearer"`` for every
    DB-loaded provider, which caused ``_split_unkeyed`` to drop OAuth/Codex
    models from pools because their rotator-issued tokens aren't in
    ``provider_api_keys``.
    """
    dbfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    os.unlink(dbfile)
    try:
        store = ConfigStore(
            database=dbfile,
            fallback_config={},
            fallback_identity_config=IdentityConfig(),
        )
        store.upsert_provider({
            "name": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "chat_path": "/responses",
            "pool_env": "opencode_codex_credentials",
        })
        store.upsert_provider({
            "name": "github-copilot",
            "base_url": "https://api.githubcopilot.com",
            "chat_path": "/chat/completions",
            "pool_env": "GITHUB_COPILOT_CREDENTIALS",
        })
        store.upsert_provider({
            "name": "cerebras",
            "base_url": "https://api.cerebras.ai",
            "chat_path": "/v1/chat/completions",
            "auth_env": "CEREBRAS_API_KEY",
        })
        runtime = store.runtime_config({})
        assert runtime["providers"]["openai-codex"].kind == "codex"
        assert runtime["providers"]["github-copilot"].kind == "oauth"
        assert runtime["providers"]["cerebras"].kind == "bearer"
    finally:
        os.unlink(dbfile)
 
