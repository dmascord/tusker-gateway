"""Shape tests for all provider entries in PROVIDER_ENDPOINTS.

Coverage:
- registry shape for every provider entry
- auth/request-shaping for oauth vs bearer providers
- route resolution for provider-prefixed and slash-form models
- live smoke test for OpenRouter using the available real credential
"""
from __future__ import annotations

import os
import tempfile
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.app import create_app
from tusker_gateway.config import load_config
from tusker_gateway.passthrough import OAUTH_PROVIDERS, PROVIDER_ENDPOINTS, PassthroughClient
from tusker_gateway.routing import POOL_ALIASES, resolve_route, split_model

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
LOCAL_LLM_KEY = os.environ.get("LOCAL_LLM_API_KEY", "")
ZAI_KEY = os.environ.get("ZAI_API_KEY", "")


REQUIRED_KEYS = {"base_url", "chat_path", "auth_type"}
VALID_AUTH_TYPES = {"oauth", "bearer", "codex"}


def test_provider_endpoints_have_required_shape():
    assert len(PROVIDER_ENDPOINTS) >= 7
    for provider, endpoint in PROVIDER_ENDPOINTS.items():
        assert isinstance(provider, str) and provider
        assert isinstance(endpoint, dict)
        missing = REQUIRED_KEYS - set(endpoint.keys())
        assert not missing, f"{provider!r} missing {missing}"
        assert endpoint["auth_type"] in VALID_AUTH_TYPES


def test_oauth_providers_match_registry():
    expected = {p for p, e in PROVIDER_ENDPOINTS.items() if e.get("auth_type") in ("oauth", "codex")}
    assert OAUTH_PROVIDERS == expected


def test_bearer_providers_no_model_header():
    for provider, endpoint in PROVIDER_ENDPOINTS.items():
        if endpoint.get("auth_type") == "bearer":
            assert not endpoint.get("model_header"), f"{provider!r} should not define model_header"


def test_oauth_providers_have_model_header():
    for provider, endpoint in PROVIDER_ENDPOINTS.items():
        if endpoint.get("auth_type") == "oauth":
            assert endpoint.get("model_header"), f"{provider!r} needs model_header"


class DummyQualityDB:
    async def record(self, *_args, **_kwargs):
        return None


def _make_client(provider_keys: dict[str, str] | None = None) -> PassthroughClient:
    app_cfg = {
        "api_keys": ["test-key"],
        "provider_api_keys": provider_keys or {},
        "codex_credentials": [],
    }
    session = aiohttp.ClientSession()
    return PassthroughClient(app_cfg, DummyQualityDB(), session)


@pytest.mark.asyncio
async def test_github_copilot_request_shape():
    client = _make_client()
    try:
        headers, body = await client._build_request(  # noqa: SLF001
            provider="github-copilot",
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            api_key=None,
            extra_headers=None,
            extra_body=None,
            endpoint=PROVIDER_ENDPOINTS["github-copilot"],
        )
        assert "Authorization" not in headers
        assert headers.get("x-github-gpt-model") == "gpt-5.5"
        assert body["model"] == "gpt-5.5"
    finally:
        await client._http.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_openai_codex_request_shape():
    client = _make_client()
    try:
        headers, body = await client._build_request(  # noqa: SLF001
            provider="openai-codex",
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            api_key=None,
            extra_headers=None,
            extra_body=None,
            endpoint=PROVIDER_ENDPOINTS["openai-codex"],
        )
        assert "Authorization" not in headers
        assert headers.get("x-openai-gpt-model") == "gpt-5.6-luna"
        assert body["model"] == "gpt-5.6-luna"
    finally:
        await client._http.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_openai_request_shape():
    client = _make_client({"openai": "sk-test"})
    try:
        headers, body = await client._build_request(  # noqa: SLF001
            provider="openai",
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            api_key=None,
            extra_headers=None,
            extra_body=None,
            endpoint=PROVIDER_ENDPOINTS["openai"],
        )
        assert headers.get("Authorization") == "Bearer sk-test"
        assert body["model"] == "gpt-4o-mini"
    finally:
        await client._http.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_openrouter_request_shape():
    client = _make_client({"openrouter": "sk-or-test"})
    try:
        headers, body = await client._build_request(  # noqa: SLF001
            provider="openrouter",
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            api_key=None,
            extra_headers=None,
            extra_body=None,
            endpoint=PROVIDER_ENDPOINTS["openrouter"],
        )
        assert headers.get("Authorization") == "Bearer sk-or-test"
        assert body["model"] == "openai/gpt-4o-mini"
    finally:
        await client._http.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_build_request_forwards_extra_body_max_tokens():
    """max_tokens in extra_body must land in the upstream request body.

    Without this, upstream providers fall back to their own default
    max_tokens (often 256-1024) and silently truncate the model mid-task.
    """
    client = _make_client({"openai": "sk-test"})
    try:
        headers, body = await client._build_request(  # noqa: SLF001
            provider="openai",
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            api_key=None,
            extra_headers=None,
            extra_body={"max_tokens": 16384, "temperature": 0.7, "top_p": 0.9},
            endpoint=PROVIDER_ENDPOINTS["openai"],
        )
        assert body["max_tokens"] == 16384
        assert body["temperature"] == 0.7
        assert body["top_p"] == 0.9
    finally:
        await client._http.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_provider_api_key_fallback_to_config():
    client = _make_client({"openai": "sk-config-key"})
    try:
        headers, _ = await client._build_request(  # noqa: SLF001
            provider="openai",
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            api_key=None,
            extra_headers=None,
            extra_body=None,
            endpoint=PROVIDER_ENDPOINTS["openai"],
        )
        assert headers.get("Authorization") == "Bearer sk-config-key"
    finally:
        await client._http.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_explicit_api_key_overrides_provider_config():
    client = _make_client({"openai": "sk-config-key"})
    try:
        headers, _ = await client._build_request(  # noqa: SLF001
            provider="openai",
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            api_key="sk-explicit",
            extra_headers=None,
            extra_body=None,
            endpoint=PROVIDER_ENDPOINTS["openai"],
        )
        assert headers.get("Authorization") == "Bearer sk-explicit"
    finally:
        await client._http.close()  # noqa: SLF001


def test_provider_prefix_passthrough():
    route = resolve_route("github-copilot::gpt-5.5", {})
    assert route.kind == "passthrough"
    assert route.provider == "github-copilot"
    assert route.model == "gpt-5.5"


def test_slash_form_passthrough():
    route = resolve_route("openai/gpt-4o-mini", {})
    assert route.kind == "passthrough"
    assert route.provider == "openai"
    assert route.model == "gpt-4o-mini"


def test_virtual_alias_resolves_to_pool():
    for alias, pool_name in POOL_ALIASES.items():
        route = resolve_route(alias, {})
        assert route.kind == "pool"
        assert route.pool_name == pool_name


def test_split_model_handles_both_formats():
    assert split_model("openai::gpt-4o") == ("openai", "gpt-4o")
    assert split_model("plain-model") == (None, "plain-model")
    assert split_model(None) == (None, None)


@pytest.mark.skipif(not OPENROUTER_KEY, reason="OPENROUTER_API_KEY not set")
@pytest.mark.asyncio
async def test_live_openrouter():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = load_config()
        cfg["api_keys"] = ["live-test-key"]
        cfg["provider_api_keys"] = {"openrouter": OPENROUTER_KEY}
        cfg["quality_db_path"] = os.path.join(tmpdir, "quality.db")
        app = create_app()
        app.on_startup.clear()
        app["config"] = cfg
        app["http_session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "openrouter/openai/gpt-4o-mini",
                    "messages": [{"role": "user", "content": "Say exactly: OR_LIVE_OK"}],
                },
                headers={"Authorization": "Bearer live-test-key"},
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            assert "OR_LIVE_OK" in data["choices"][0]["message"]["content"]
        finally:
            await client.close()


@pytest.mark.skipif(not GROQ_KEY, reason="GROQ_API_KEY not set")
@pytest.mark.asyncio
async def test_live_groq():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = load_config()
        cfg["api_keys"] = ["live-test-key"]
        cfg["provider_api_keys"] = {"groq": GROQ_KEY}
        cfg["quality_db_path"] = os.path.join(tmpdir, "quality.db")
        app = create_app()
        app.on_startup.clear()
        app["config"] = cfg
        app["http_session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "groq/openai/gpt-oss-20b",
                    "messages": [{"role": "user", "content": "Say exactly: GROQ_LIVE_OK"}],
                },
                headers={"Authorization": "Bearer live-test-key"},
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            assert "GROQ_LIVE_OK" in data["choices"][0]["message"]["content"]
        finally:
            await client.close()


@pytest.mark.skipif(not LOCAL_LLM_KEY, reason="LOCAL_LLM_API_KEY not set")
@pytest.mark.asyncio
async def test_live_local_llm():
    pytest.skip("No confirmed local-llm endpoint/model mapping yet")


@pytest.mark.skipif(not ZAI_KEY, reason="ZAI_API_KEY not set")
@pytest.mark.asyncio
async def test_live_zai():
    pytest.skip("No confirmed ZAI endpoint/model mapping yet")