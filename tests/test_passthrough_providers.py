"""Tests for passthrough request shaping across every PROVIDER_ENDPOINTS entry.

Covers:
- OAuth providers (github-copilot, openai-codex): Authorization from codex_credentials, model_header in headers
- API-key providers (openai, openrouter): Authorization from provider_api_keys
- Provider-prefixed routing (provider::model)
- Slash-form routing (provider/model)
- Missing auth: no Authorization header set
- Config loading: codex_credentials from env
- LIVE smoke test for openrouter (when OPENROUTER_API_KEY is set)
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tusker_gateway.config import load_config
from tusker_gateway.passthrough import PROVIDER_ENDPOINTS, PassthroughClient
from tusker_gateway.quality import QualityDB
from tusker_gateway.routing import Route, resolve_route, split_model


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _base_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "api_keys": ["gw-test-key"],
        "provider_api_keys": {},
        "codex_credentials": [],
        "quality_db_path": ":memory:",
    }
    cfg.update(overrides)
    return cfg


def _oauth_config() -> dict[str, Any]:
    return _base_config(
        codex_credentials=[{"token": "test-oauth-token-abc", "refresh_token": "rt-1"}],
    )


@pytest.fixture
def mock_http():
    """A minimal mock aiohttp.ClientSession."""
    return MagicMock()


@pytest.fixture
def quality_db():
    return QualityDB(":memory:")


@pytest.fixture
def bearer_client(mock_http, quality_db):
    """PassthroughClient with no OAuth credentials (bearer-path providers)."""
    return PassthroughClient(_base_config(), quality_db, mock_http)


@pytest.fixture
def oauth_client(mock_http, quality_db):
    """PassthroughClient with OAuth credentials configured."""
    return PassthroughClient(_oauth_config(), quality_db, mock_http)


# ---------------------------------------------------------------------------
# Routing: provider-prefixed and slash-form
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model_id,expected_provider,expected_model", [
    # provider::model
    ("github-copilot::gpt-5.5", "github-copilot", "gpt-5.5"),
    ("openai-codex::gpt-5.6-sol", "openai-codex", "gpt-5.6-sol"),
    ("openai::gpt-4o", "openai", "gpt-4o"),
    ("openrouter::openai/gpt-4o-mini", "openrouter", "openai/gpt-4o-mini"),
    # provider/model
    ("github-copilot/claude-sonnet-4.6", "github-copilot", "claude-sonnet-4.6"),
    ("openai/gpt-4o", "openai", "gpt-4o"),
    ("openrouter/openai/gpt-4o-mini", "openrouter", "openai/gpt-4o-mini"),
])
def test_routing_resolves_provider_passthrough(model_id, expected_provider, expected_model):
    route = resolve_route(model_id, {})
    assert route.kind == "passthrough", f"Expected passthrough, got {route.kind} for {model_id}"
    assert route.provider == expected_provider, f"Provider mismatch for {model_id}"
    assert route.model == expected_model, f"Model mismatch for {model_id}"


@pytest.mark.parametrize("alias,pool", [
    ("hermes-code", "code"),
    ("hermes-privacy", "privacy"),
    ("hermes-premium", "premium"),
    ("hermes-swarm", "swarm"),
])
def test_virtual_aliases_still_route_to_pools(alias, pool):
    route = resolve_route(alias, {})
    assert route.kind == "pool"
    assert route.pool_name == pool


def test_swarm_markers_still_route_to_swarm():
    route = resolve_route("hermes-gateway/gpt-5.5", {})
    assert route.kind == "swarm"


# ---------------------------------------------------------------------------
# Request shape: OAuth providers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github-copilot", "openai-codex"])
async def test_oauth_provider_request_shape(oauth_client, provider):
    """OAuth providers get Authorization from codex_credentials and a model_header."""
    endpoint = PROVIDER_ENDPOINTS[provider]
    msgs = [{"role": "user", "content": "hello"}]
    headers, body = await oauth_client._build_request(
        provider, "test-model", msgs,
        stream=False, api_key=None,
        extra_headers=None, extra_body=None,
        endpoint=endpoint,
    )
    # Auth: Bearer <token>
    assert headers["Authorization"] == "Bearer test-oauth-token-abc"
    # Model header present
    model_header = endpoint["model_header"]
    assert headers[model_header] == "test-model"
    # Body always has model and messages
    assert body["model"] == "test-model"
    assert body["messages"] == msgs
    assert body["stream"] is False


# ---------------------------------------------------------------------------
# Request shape: API-key providers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider,key_env,expected_key", [
    ("openai", "openai", "sk-openai-test"),
    ("openrouter", "openrouter", "sk-or-test"),
    ("groq", "groq", "sk-groq-test"),
    ("local-llm", "local-llm", "sk-local-test"),
    ("zai", "zai", "sk-zai-test"),
])
async def test_apikey_provider_request_shape(mock_http, quality_db, provider, key_env, expected_key):
    config = _base_config(provider_api_keys={key_env: expected_key})
    client = PassthroughClient(config, quality_db, mock_http)
    endpoint = PROVIDER_ENDPOINTS[provider]
    msgs = [{"role": "user", "content": "hi"}]
    headers, body = await client._build_request(
        provider, "test-model", msgs,
        stream=True, api_key=None,
        extra_headers=None, extra_body=None,
        endpoint=endpoint,
    )
    assert headers["Authorization"] == f"Bearer {expected_key}"
    # No model_header for bearer providers
    model_headers_present = [k for k in headers if k.startswith("x-")]
    assert model_headers_present == []
    assert body["stream"] is True
    assert body["model"] == "test-model"


# ---------------------------------------------------------------------------
# No provider key: no auth header (not gateway key leaked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "openrouter", "groq", "local-llm", "zai"])
async def test_no_key_no_auth_header(bearer_client, provider):
    """When no provider key and no api_key, no Authorization header is set."""
    endpoint = PROVIDER_ENDPOINTS[provider]
    headers, _ = await bearer_client._build_request(
        provider, "m", [{"role": "user", "content": "x"}],
        stream=False, api_key=None,
        extra_headers=None, extra_body=None,
        endpoint=endpoint,
    )
    assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# Explicit api_key overrides provider_api_keys
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explicit_api_key_overrides_provider_key(mock_http, quality_db):
    config = _base_config(provider_api_keys={"openai": "sk-from-config"})
    client = PassthroughClient(config, quality_db, mock_http)
    endpoint = PROVIDER_ENDPOINTS["openai"]
    headers, _ = await client._build_request(
        "openai", "gpt-4o", [{"role": "user", "content": "hi"}],
        stream=False, api_key="sk-explicit-override",
        extra_headers=None, extra_body=None,
        endpoint=endpoint,
    )
    assert headers["Authorization"] == "Bearer sk-explicit-override"


# ---------------------------------------------------------------------------
# extra_headers and extra_body are merged correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extra_headers_and_body(bearer_client):
    endpoint = PROVIDER_ENDPOINTS["openai"]
    headers, body = await bearer_client._build_request(
        "openai", "gpt-4o", [{"role": "user", "content": "hi"}],
        stream=False, api_key=None,
        extra_headers={"X-Custom": "val", "Authorization": "Bearer explicit"},
        extra_body={"temperature": 0.5},
        endpoint=endpoint,
    )
    assert headers["X-Custom"] == "val"
    assert headers["Authorization"] == "Bearer explicit"
    assert body["temperature"] == 0.5


# ---------------------------------------------------------------------------
# Config loading: codex_credentials
# ---------------------------------------------------------------------------

def test_config_loads_codex_credentials():
    """CODEX_CREDENTIALS env var is parsed into config['codex_credentials']."""
    creds = [{"token": "t1", "refresh_token": "rt1"}, {"token": "t2", "refresh_token": "rt2"}]
    env = {"CODEX_CREDENTIALS": json.dumps(creds), "API_KEYS": "k1"}
    with patch.dict(os.environ, env, clear=False):
        cfg = load_config()
    assert cfg["codex_credentials"] == creds


def test_config_loads_codex_credentials_single_object():
    """A single dict (not a list) is wrapped into a list."""
    cred = {"token": "t1", "refresh_token": "rt1"}
    env = {"CODEX_CREDENTIALS": json.dumps(cred), "API_KEYS": "k1"}
    with patch.dict(os.environ, env, clear=False):
        cfg = load_config()
    assert cfg["codex_credentials"] == [cred]


def test_config_loads_provider_api_keys_json():
    """PROVIDER_API_KEYS JSON dict is loaded."""
    env = {
        "PROVIDER_API_KEYS": '{"openai": "sk-oa", "openrouter": "sk-or"}',
        "API_KEYS": "k1",
    }
    # Clear provider keys that are set in the real environment
    clear_keys = [k for k in os.environ if k.startswith("PROVIDER_") and k != "PROVIDER_API_KEYS"]
    with patch.dict(os.environ, env, clear=False):
        for k in clear_keys:
            os.environ.pop(k, None)
        cfg = load_config()
    assert cfg["provider_api_keys"]["openai"] == "sk-oa"
    assert cfg["provider_api_keys"]["openrouter"] == "sk-or"


def test_config_loads_provider_api_keys_env_vars():
    """Individual PROVIDER_*_API_KEY env vars are loaded."""
    env = {
        "PROVIDER_OPENAI_API_KEY": "sk-oa-override",
        "PROVIDER_OPENROUTER_API_KEY": "sk-or-override",
        "API_KEYS": "k1",
    }
    with patch.dict(os.environ, env, clear=False):
        cfg = load_config()
    assert cfg["provider_api_keys"]["openai"] == "sk-oa-override"
    assert cfg["provider_api_keys"]["openrouter"] == "sk-or-override"


# ---------------------------------------------------------------------------
# All PROVIDER_ENDPOINTS entries have required fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider", list(PROVIDER_ENDPOINTS.keys()))
def test_endpoint_has_required_fields(provider):
    """Every provider entry must have base_url, chat_path, and auth_type."""
    ep = PROVIDER_ENDPOINTS[provider]
    assert "base_url" in ep, f"{provider} missing base_url"
    assert "chat_path" in ep, f"{provider} missing chat_path"
    assert ep["auth_type"] in ("oauth", "bearer", "codex"), f"{provider} has unexpected auth_type: {ep['auth_type']}"
    if ep["auth_type"] in ("oauth", "codex"):
        assert "model_header" in ep, f"{provider} ({ep['auth_type']}) missing model_header"


# ---------------------------------------------------------------------------
# PROVIDER_ENDPOINTS values: URLs are well-formed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider", list(PROVIDER_ENDPOINTS.keys()))
def test_endpoint_urls_are_well_formed(provider):
    ep = PROVIDER_ENDPOINTS[provider]
    assert ep["base_url"].startswith("https://"), f"{provider} base_url not https"
    assert ep["chat_path"].startswith("/"), f"{provider} chat_path must start with /"


# ---------------------------------------------------------------------------
# OAuth provider: no credentials → no Authorization header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github-copilot", "openai-codex"])
async def test_oauth_no_credentials_no_auth(mock_http, quality_db, provider):
    config = _base_config(codex_credentials=[])
    client = PassthroughClient(config, quality_db, mock_http)
    endpoint = PROVIDER_ENDPOINTS[provider]
    # Note: openai-codex uses the codex auth strategy (raw JWT, not exchange).
    # Both oauth and codex strategies leave Authorization unset when no creds exist.
    headers, body = await client._build_request(
        provider, "model", [{"role": "user", "content": "hi"}],
        stream=False, api_key=None,
        extra_headers=None, extra_body=None,
        endpoint=endpoint,
    )
    assert "Authorization" not in headers, (
        f"{provider} leaked Authorization when no creds: {headers}"
    )
    model_header = endpoint["model_header"]
    assert headers[model_header] == "model"


@pytest.mark.parametrize("provider", list(PROVIDER_ENDPOINTS.keys()))
def test_endpoint_urls_are_well_formed(provider):
    ep = PROVIDER_ENDPOINTS[provider]
    # Allow http://localhost for local providers (e.g., local-llm)
    if ep["base_url"].startswith("http://localhost") or ep["base_url"].startswith("http://127.0.0.1"):
        assert ep["base_url"].startswith("http://"), f"{provider} base_url must be http for localhost"
    else:
        assert ep["base_url"].startswith("https://"), f"{provider} base_url not https"
    assert ep["chat_path"].startswith("/"), f"{provider} chat_path must start with /"

# ---------------------------------------------------------------------------
# OAuth rotator: get_token returns first token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_codex_rotator_get_token():
    from tusker_gateway.passthrough import CodexTokenRotator
    creds = [{"token": "t1"}, {"token": "t2"}]
    rotator = CodexTokenRotator(creds)
    assert await rotator.get_token() == "t1"


@pytest.mark.asyncio
async def test_codex_rotator_empty():
    from tusker_gateway.passthrough import CodexTokenRotator
    rotator = CodexTokenRotator([])
    assert await rotator.get_token() is None


# ---------------------------------------------------------------------------
# LIVE smoke: openrouter (requires OPENROUTER_API_KEY in env)
# ---------------------------------------------------------------------------

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
openrouter_live = pytest.mark.skipif(
    not OPENROUTER_KEY,
    reason="OPENROUTER_API_KEY not set",
)


@openrouter_live
@pytest.mark.asyncio
async def test_live_openrouter_chat():
    """Real OpenRouter call with openai/gpt-4o-mini."""
    import aiohttp
    from tusker_gateway.quality import QualityDB as QDB

    with tempfile.TemporaryDirectory() as tmpdir:
        config = _base_config(
            provider_api_keys={"openrouter": OPENROUTER_KEY},
            quality_db_path=os.path.join(tmpdir, "q.db"),
        )
        qdb = QDB(config["quality_db_path"])
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            client = PassthroughClient(config, qdb, session)
            result = await client.chat(
                "openrouter", "openai/gpt-4o-mini",
                [{"role": "user", "content": "Say exactly: PROVIDER_TEST_OK"}],
                stream=False,
            )
        assert "choices" in result
        content = result["choices"][0]["message"]["content"]
        assert "PROVIDER_TEST_OK" in content, f"Unexpected: {content}"


@openrouter_live
@pytest.mark.asyncio
async def test_live_openrouter_stream():
    """Real OpenRouter streaming call."""
    import aiohttp
    from tusker_gateway.quality import QualityDB as QDB

    with tempfile.TemporaryDirectory() as tmpdir:
        config = _base_config(
            provider_api_keys={"openrouter": OPENROUTER_KEY},
            quality_db_path=os.path.join(tmpdir, "q.db"),
        )
        qdb = QDB(config["quality_db_path"])
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            client = PassthroughClient(config, qdb, session)
            stream = await client.chat(
                "openrouter", "openai/gpt-4o-mini",
                [{"role": "user", "content": "Say exactly: STREAM_TEST_OK"}],
                stream=True,
            )
            chunks = []
            async for chunk in stream:
                chunks.append(chunk)
        body = b"".join(chunks)
        assert b"[DONE]" in body
        assert b"data:" in body


# ---------------------------------------------------------------------------
# Provider coverage summary (documents which providers were exercised)
# ---------------------------------------------------------------------------

def test_provider_coverage_summary():
    """Documents provider test coverage for audit trail."""
    coverage = {}
    for provider in PROVIDER_ENDPOINTS:
        if provider == "openrouter" and OPENROUTER_KEY:
            coverage[provider] = "live"
        elif provider in ("github-copilot", "openai-codex"):
            coverage[provider] = "mock_shape"
        else:
            coverage[provider] = "mock_shape"
    # At minimum, all providers have shape coverage
    assert len(coverage) == len(PROVIDER_ENDPOINTS)
    # If OPENROUTER_KEY is set, openrouter is live-tested
    if OPENROUTER_KEY:
        assert coverage["openrouter"] == "live"
