"""Tests for authentication strategies — especially the ApiKeyHeaderAuthenticator."""
from __future__ import annotations

import pytest

from tusker_gateway.auth_strategies import (
    ApiKeyHeaderAuthenticator,
    BearerAuthenticator,
    get_auth_strategy,
)
from tusker_gateway.models import ProviderEndpoint


# ── Fixtures ────────────────────────────────────────────────────────────────


def _ep(**overrides: object) -> ProviderEndpoint:
    """Build a minimal ProviderEndpoint with sensible defaults."""
    kw = dict(base_url="https://example.com", chat_path="/v1/chat/completions")
    kw.update(overrides)
    return ProviderEndpoint(**kw)  # type: ignore[arg-type]


# ── BearerAuthenticator (baseline) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_bearer_explicit_key():
    ep = _ep()
    headers = await BearerAuthenticator().headers({}, "openai", "gpt-4o", "sk-abc", ep)
    assert headers == {"Authorization": "Bearer sk-abc"}


@pytest.mark.asyncio
async def test_bearer_fallback_to_provider_api_keys():
    ep = _ep()
    config = {"provider_api_keys": {"openai": "sk-from-config"}}
    headers = await BearerAuthenticator().headers(config, "openai", "gpt-4o", None, ep)
    assert headers == {"Authorization": "Bearer sk-from-config"}


@pytest.mark.asyncio
async def test_bearer_no_key():
    ep = _ep()
    headers = await BearerAuthenticator().headers({}, "openai", "gpt-4o", None, ep)
    assert headers == {}


# ── ApiKeyHeaderAuthenticator ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_key_header_explicit_key():
    """Explicit api_key arg → sent in api-key header."""
    ep = _ep(api_key_header="api-key")
    headers = await ApiKeyHeaderAuthenticator().headers(
        {}, "apim", "gpt-5.6-luna", "sub-key-123", ep,
    )
    assert headers == {"api-key": "sub-key-123"}


@pytest.mark.asyncio
async def test_api_key_header_from_provider_api_keys():
    """No explicit api_key → fallback to provider_api_keys."""
    ep = _ep(api_key_header="api-key")
    config = {"provider_api_keys": {"apim": "sub-key-from-config"}}
    headers = await ApiKeyHeaderAuthenticator().headers(
        config, "apim", "gpt-5.6-luna", None, ep,
    )
    assert headers == {"api-key": "sub-key-from-config"}


@pytest.mark.asyncio
async def test_api_key_header_custom_header_name():
    """api_key_header field names a custom request header."""
    ep = _ep(api_key_header="Ocp-Apim-Subscription-Key")
    headers = await ApiKeyHeaderAuthenticator().headers(
        {}, "apim", "gpt-5.6-luna", "my-sub", ep,
    )
    assert headers == {"Ocp-Apim-Subscription-Key": "my-sub"}


@pytest.mark.asyncio
async def test_api_key_header_no_key():
    """No key available → no headers added."""
    ep = _ep(api_key_header="api-key")
    headers = await ApiKeyHeaderAuthenticator().headers({}, "apim", "gpt-5.6-luna", None, ep)
    assert headers == {}


@pytest.mark.asyncio
async def test_api_key_header_empty_header_name():
    """Empty api_key_header falls back to default 'api-key'."""
    ep = _ep(api_key_header="")
    headers = await ApiKeyHeaderAuthenticator().headers(
        {}, "apim", "gpt-5.6-luna", "tok", ep,
    )
    assert headers == {"api-key": "tok"}


@pytest.mark.asyncio
async def test_api_key_header_none_header_name():
    """api_key_header=None falls back to default 'api-key'."""
    ep = _ep(api_key_header=None)
    headers = await ApiKeyHeaderAuthenticator().headers(
        {}, "apim", "gpt-5.6-luna", "tok", ep,
    )
    assert headers == {"api-key": "tok"}


# ── get_auth_strategy routing ───────────────────────────────────────────────


def test_get_auth_strategy_bearer():
    assert isinstance(get_auth_strategy("bearer"), BearerAuthenticator)


def test_get_auth_strategy_api_key():
    assert isinstance(get_auth_strategy("api_key"), ApiKeyHeaderAuthenticator)


def test_get_auth_strategy_unknown_falls_to_bearer():
    assert isinstance(get_auth_strategy("something-else"), BearerAuthenticator)


# ── ProviderEndpoint threading ──────────────────────────────────────────────


def test_provider_endpoint_from_raw_includes_api_key_header():
    raw = {
        "base_url": "https://apim.example.com",
        "chat_path": "/chat/completions",
        "auth_type": "api_key",
        "api_key_header": "api-key",
    }
    ep = ProviderEndpoint.from_raw(raw)
    assert ep.auth_type == "api_key"
    assert ep.api_key_header == "api-key"


def test_provider_endpoint_to_raw_includes_api_key_header():
    ep = _ep(auth_type="api_key", api_key_header="api-key")
    raw = ep.to_raw()
    assert raw["api_key_header"] == "api-key"


def test_provider_endpoint_from_raw_without_api_key_header():
    raw = {"base_url": "https://x.com", "chat_path": "/chat", "auth_type": "bearer"}
    ep = ProviderEndpoint.from_raw(raw)
    assert ep.api_key_header is None


def test_provider_endpoint_from_registry_api_key():
    """from_registry with kind='api_key' in the allowlist."""
    from tusker_gateway.config import ProviderConfig as PC
    from dataclasses import replace

    pc = PC(
        name="test", kind="api_key", base_url="https://x.com",
        chat_path="/chat", auth_type="api_key",
        api_key_header="api-key",
    )
    registry = {"test": pc}
    ep = ProviderEndpoint.from_registry(registry, "test")
    assert ep.auth_type == "api_key"
    assert ep.api_key_header == "api-key"
