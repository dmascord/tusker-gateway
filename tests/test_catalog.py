"""Tests for the dynamic catalog refresh module.

Uses a fake aiohttp session (no real network) to exercise the parsing
logic and TTL cache behaviour.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import pytest

from tusker_gateway.catalog import (
    Catalog,
    CatalogClient,
    CatalogEntry,
    CatalogError,
    CatalogRegistry,
    CodexCatalog,
    CopilotCatalog,
    ModelsDevCatalog,
    OpenCodeCatalog,
    OpenCodeGoCatalog,
    OpenRouterCatalog,
    ProviderModelsCatalog,
    XiaomiCatalog,
    _parse_cost_field,
    advertised_input_modalities,
    advertised_tool_support,
    catalog_refresh_loop,
)


# ---------------------------------------------------------------------------
# _parse_cost_field
# ---------------------------------------------------------------------------


def test_parse_cost_field_number():
    assert _parse_cost_field(0.5) == 0.5
    assert _parse_cost_field(2) == 2.0
    assert _parse_cost_field(0) == 0.0


def test_parse_cost_field_string_with_unit():
    """models.dev format: '0.5 / 1M tokens'."""
    assert _parse_cost_field("0.5 / 1M tokens") == 0.5
    assert _parse_cost_field("3 / 1M") == 3.0
    assert _parse_cost_field("0.1234 / 1M tokens") == 0.1234


def test_parse_cost_field_none_or_invalid():
    assert _parse_cost_field(None) is None
    assert _parse_cost_field("not-a-number / 1M") is None
    assert _parse_cost_field([]) is None
    assert _parse_cost_field({}) is None


def test_catalog_capability_helpers_read_openrouter_shape():
    entry = CatalogEntry(
        provider="openrouter",
        model="nvidia/test",
        raw={
            "architecture": {
                "input_modalities": ["text", "image"],
                "modality": "text+image->text",
            },
            "supported_parameters": ["max_tokens", "tools", "tool_choice"],
        },
    )
    assert advertised_input_modalities(entry) == frozenset({"text", "image"})
    assert advertised_tool_support(entry) is True

    no_tools = CatalogEntry(
        provider="openrouter",
        model="nvidia/no-tools",
        raw={"supported_parameters": ["max_tokens", "temperature"]},
    )
    assert advertised_tool_support(no_tools) is False
    assert advertised_tool_support(CatalogEntry(provider="x", model="unknown")) is None


# ---------------------------------------------------------------------------
# CodexCatalog.fetch
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]):
        self._responses = responses

    def get(self, url: str, **kw):
        return self._responses.get(url) or self._responses["default"]


@pytest.mark.asyncio
async def test_codex_catalog_parses_models():
    body = {
        "models": [
            {"slug": "gpt-5.6-sol", "visibility": "list"},
            {"slug": "gpt-5.6-terra", "visibility": "list"},
            {"slug": "gpt-reserve", "visibility": "hide"},   # should be dropped
            {"slug": "gpt-5.6-luna", "visibility": "list"},
        ]
    }
    session = FakeSession({"default": FakeResponse(200, body)})
    cat = CodexCatalog()
    entries = await cat.fetch(session)
    slugs = {e.model for e in entries}
    assert "gpt-5.6-sol" in slugs
    assert "gpt-5.6-terra" in slugs
    assert "gpt-5.6-luna" in slugs
    assert "gpt-reserve" not in slugs  # visibility=hide filtered


@pytest.mark.asyncio
async def test_codex_catalog_handles_non_200():
    session = FakeSession({"default": FakeResponse(503, {"error": "down"})})
    cat = CodexCatalog()
    with pytest.raises(CatalogError, match="HTTP 503"):
        await cat.fetch(session)


@pytest.mark.asyncio
async def test_codex_catalog_skips_invalid_entries():
    """Empty slugs / non-dict entries are silently dropped."""
    body = {
        "models": [
            {"slug": ""},                  # empty slug
            {"slug": 123},                 # wrong type
            "not-a-dict",                  # not a dict
            {"slug": "valid-model"},       # ok
        ]
    }
    session = FakeSession({"default": FakeResponse(200, body)})
    cat = CodexCatalog()
    entries = await cat.fetch(session)
    assert len(entries) == 1
    assert entries[0].model == "valid-model"


# ---------------------------------------------------------------------------
# CopilotCatalog.fetch (returns dual provider entries)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copilot_catalog_emits_single_provider_entries():
    body = {
        "data": [
            {"id": "gpt-5.5"},
            {"id": "claude-sonnet-4.6"},
        ]
    }
    session = FakeSession({"default": FakeResponse(200, body)})
    cat = CopilotCatalog(provider="github-copilot")
    entries = await cat.fetch(session)
    providers = {(e.provider, e.model) for e in entries}
    assert ("github-copilot", "gpt-5.5") in providers
    assert ("github-copilot", "claude-sonnet-4.6") in providers
    assert all(e.provider == "github-copilot" for e in entries)


@pytest.mark.asyncio
async def test_provider_models_catalog_parses_standard_and_ollama_shapes():
    session = _CapturingSession(FakeResponse(200, {
        "models": [
            {"id": "provider-model", "pricing": {"input": "0", "output": "0"}},
            {"name": "second-model"},
            {"id": ""},
        ],
    }))
    catalog = ProviderModelsCatalog(
        provider="cerebras",
        endpoint="https://api.example.test/v1/models",
    )
    catalog.set_api_key("provider-key")

    entries = await catalog.fetch(session)

    assert [(entry.provider, entry.model) for entry in entries] == [
        ("cerebras", "provider-model"),
        ("cerebras", "second-model"),
    ]
    assert entries[0].cost_input == 0.0
    assert entries[0].cost_output == 0.0
    assert session.captured["headers"]["Authorization"] == "Bearer provider-key"


def test_registry_default_includes_provider_native_catalogs():
    registry = CatalogRegistry.default()

    for provider in (
        "openai", "groq", "zai", "arliai", "google", "cerebras",
        "cohere", "minimax", "synthetic", "ollama-cloud", "local-llm",
        "nvidia",
    ):
        client = registry.get_client(provider)
        assert isinstance(client, ProviderModelsCatalog)
        assert client.endpoint


def test_registry_default_honors_custom_provider_model_endpoint():
    registry = CatalogRegistry.default({
        "custom": {
            "base_url": "https://provider.example.test/v1",
            "chat_path": "/chat/completions",
            "models_path": "/models",
        },
    })

    client = registry.get_client("custom")
    assert isinstance(client, ProviderModelsCatalog)
    assert client.endpoint == "https://provider.example.test/v1/models"

class _CapturingSession:
    """Minimal aiohttp-like session that records the headers of each GET."""

    def __init__(self, response):
        self._response = response
        self.captured: dict[str, Any] = {}

    def get(self, url, **kw):
        self.captured["url"] = url
        self.captured["headers"] = kw.get("headers", {})
        return self._response


@pytest.mark.asyncio
async def test_codex_catalog_uses_token_source_auth_headers():
    """With a token_source configured, CodexCatalog.fetch must send the
    Codex Responses API auth header set (Bearer + Cloudflare bypass +
    ChatGPT-Account-ID from the JWT) so the endpoint stops returning
    HTTP 401.
    """
    # Build a fake JWT with a chatgpt_account_id claim so the helper
    # populates ChatGPT-Account-ID. Three parts: header.payload.sig.
    import base64
    import json
    payload = base64.urlsafe_b64encode(
        json.dumps({"chatgpt_account_id": "acct-fake-123"}).encode()
    ).rstrip(b"=").decode()
    fake_jwt = f"header.{payload}.signature"

    session = _CapturingSession(FakeResponse(200, {"models": []}))
    cat = CodexCatalog()
    cat.set_token_source(lambda: _coro(fake_jwt))
    await cat.fetch(session)

    headers = session.captured["headers"]
    assert headers["Authorization"] == f"Bearer {fake_jwt}"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["User-Agent"] == "codex_cli_rs/0.0.0 (Tusker Gateway)"
    assert headers["ChatGPT-Account-ID"] == "acct-fake-123"
    assert headers["accept"] == "application/json"


@pytest.mark.asyncio
async def test_codex_catalog_without_token_omits_authorization():
    """Without a token_source or static key, the request must not
    include an Authorization header (so callers see the upstream 401
    rather than a malformed request).
    """
    session = _CapturingSession(FakeResponse(401, {"error": "unauth"}))
    cat = CodexCatalog()
    with pytest.raises(CatalogError):
        await cat.fetch(session)
    headers = session.captured["headers"]
    assert "Authorization" not in headers
    assert headers["accept"] == "application/json"


@pytest.mark.asyncio
async def test_copilot_catalog_exchanges_token_and_sends_copilot_headers(monkeypatch):
    """CopilotCatalog.fetch must exchange the raw GitHub token via the
    Copilot token-exchange endpoint and emit the canonical Copilot
    request header set (Editor-Version, Copilot-Integration-Id, etc.)
    so the catalog endpoint stops returning HTTP 400.
    """
    exchanged = "exch-token"
    raw = "gho_test"

    async def fake_exchange(raw_token, **kw):
        assert raw_token == raw
        return exchanged, 9_999_999_999.0

    # Patch at the source module so the catalog fetch (which imports it
 # inside fetch) sees the mock.
    monkeypatch.setattr(
        "tusker_gateway.copilot_exchange.exchange_copilot_token", fake_exchange
    )
    # copilot_request_headers reads an env override; ensure clean state.
    monkeypatch.delenv("GITHUB_COPILOT_INTEGRATION_ID", raising=False)

    session = _CapturingSession(FakeResponse(200, {"data": []}))
    cat = CopilotCatalog(provider="github-copilot")
    cat.set_token_source(lambda: _coro(raw))
    await cat.fetch(session)

    headers = session.captured["headers"]
    assert headers["Authorization"] == f"Bearer {exchanged}"
    assert headers["Editor-Version"]  # from copilot_request_headers
    assert "Copilot-Integration-Id" in headers
    assert "x-initiator" in headers


async def _coro(value):
    return value


# ---------------------------------------------------------------------------
# OpenRouterCatalog.fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_catalog_parses_models():
    body = {
        "data": [
            {"id": "openai/gpt-oss-20b:free"},
            {"id": "google/gemma-4-31b-it:free"},
        ]
    }
    session = FakeSession({"default": FakeResponse(200, body)})
    cat = OpenRouterCatalog()
    entries = await cat.fetch(session)
    assert {e.model for e in entries} == {
        "openai/gpt-oss-20b:free",
        "google/gemma-4-31b-it:free",
    }
    assert all(e.provider == "openrouter" for e in entries)
    # Default test body has no pricing fields — cost_* should be None.
    for e in entries:
        assert e.cost_input is None
        assert e.cost_output is None


@pytest.mark.asyncio
async def test_openrouter_catalog_extracts_pricing():
    """Free-tier entries (pricing.prompt == "0") get cost_input == 0;
    paid entries get the parsed dollar value."""
    body = {
        "data": [
            {"id": "stealth/ox-alpha", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "anthropic/claude-sonnet-4.6", "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
        ]
    }
    session = FakeSession({"default": FakeResponse(200, body)})
    entries = await OpenRouterCatalog().fetch(session)
    by_model = {e.model: e for e in entries}
    assert by_model["stealth/ox-alpha"].cost_input == 0.0
    assert by_model["stealth/ox-alpha"].cost_output == 0.0
    assert by_model["anthropic/claude-sonnet-4.6"].cost_output == pytest.approx(1.5e-5)

@pytest.mark.asyncio
async def test_opencode_zen_catalog_parses_models():
    body = {"data": [{"id": "muse-spark-1.2"}, {"id": "big-pickle"}]}
    session = FakeSession({"default": FakeResponse(200, body)})
    entries = await OpenCodeCatalog().fetch(session)
    assert {e.model for e in entries} == {"muse-spark-1.2", "big-pickle"}
    assert all(e.provider == "opencode-zen" for e in entries)


@pytest.mark.asyncio
async def test_opencode_go_catalog_parses_models():
    body = {"data": [{"id": "minimax-m3"}, {"id": "kimi-k2.6"}]}
    session = FakeSession({"default": FakeResponse(200, body)})
    entries = await OpenCodeGoCatalog().fetch(session)
    assert {e.model for e in entries} == {"minimax-m3", "kimi-k2.6"}
    assert all(e.provider == "opencode-go" for e in entries)
    # Go subclass should hit /go/v1/models, not /v1/models.
    assert OpenCodeGoCatalog.ENDPOINT.endswith("/go/v1/models")


# ---------------------------------------------------------------------------
# XiaomiCatalog.fetch
# ---------------------------------------------------------------------------


class RecordingSession(FakeSession):
    def __init__(self, responses: dict[str, FakeResponse]):
        super().__init__(responses)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kw):
        self.requests.append((url, kw))
        return super().get(url, **kw)


@pytest.mark.asyncio
async def test_xiaomi_catalog_uses_authenticated_token_plan_endpoint_and_ttl():
    session = RecordingSession({
        XiaomiCatalog.ENDPOINT: FakeResponse(200, {"data": []}),
    })
    catalog = XiaomiCatalog()
    catalog.set_api_key("tp-test-key")

    await catalog.fetch(session)

    assert (
        XiaomiCatalog.ENDPOINT
        == "https://token-plan-sgp.xiaomimimo.com/v1/models"
    )
    assert catalog.ttl_secs == 3600.0
    assert session.requests == [(
        XiaomiCatalog.ENDPOINT,
        {"headers": {
            "User-Agent": "tusker-gateway/1.0 (catalog-refresh)",
            "accept": "application/json",
            "Authorization": "Bearer tp-test-key",
        }},
    )]


@pytest.mark.asyncio
async def test_xiaomi_catalog_filters_speech_and_enriches_chat_models():
    body = {
        "data": [
            {"id": "mimo-v2.5", "object": "model", "owned_by": "xiaomi"},
            {"id": "mimo-v2.5-pro", "object": "model", "owned_by": "xiaomi"},
            {"id": "mimo-v2.5-asr", "object": "model"},
            {"id": "mimo-v2.5-tts", "object": "model"},
            {"id": "mimo-v2.5-tts-hd", "object": "model"},
            {"id": "mimo-v2.5-embedding", "object": "model"},
        ],
    }

    entries = await XiaomiCatalog().fetch(
        FakeSession({"default": FakeResponse(200, body)})
    )
    by_model = {entry.model: entry for entry in entries}

    # Speech and other non-chat rows have no modality metadata upstream and
    # must not enter chat pools.
    assert set(by_model) == {"mimo-v2.5", "mimo-v2.5-pro"}
    assert all(entry.provider == "xiaomi" for entry in entries)
    assert by_model["mimo-v2.5"].input_modalities == frozenset(
        {"text", "image"}
    )
    assert by_model["mimo-v2.5-pro"].input_modalities == frozenset({"text"})
    assert (
        by_model["mimo-v2.5"].cost_input,
        by_model["mimo-v2.5"].cost_output,
    ) == (0.14, 0.28)
    assert (
        by_model["mimo-v2.5-pro"].cost_input,
        by_model["mimo-v2.5-pro"].cost_output,
    ) == (0.435, 0.87)


def test_app_catalog_key_wiring_injects_xiaomi_key():
    from tusker_gateway.app import _wire_catalog_api_keys

    registry = CatalogRegistry.default()
    _wire_catalog_api_keys(registry, {"xiaomi": "tp-startup-key"})

    xiaomi = registry.get_client("xiaomi")
    assert xiaomi is not None
    assert xiaomi._auth_headers()["Authorization"] == "Bearer tp-startup-key"


def test_catalog_client_set_api_key_injects_auth():
    """set_api_key enables Authorization header via _auth_headers."""
    client = CatalogClient()
    assert "Authorization" not in client._auth_headers({})
    client.set_api_key("sk-test-1234")
    headers = client._auth_headers({"User-Agent": "test"})
    assert headers["Authorization"] == "Bearer sk-test-1234"
    assert headers["User-Agent"] == "test"
    client.set_api_key(None)
    assert "Authorization" not in client._auth_headers({})


# ---------------------------------------------------------------------------
# ModelsDevCatalog.fetch (pricing parsing)
# ---------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_models_dev_catalog_extracts_pricing():
    body = {
        "anthropic": {
            "models": {
                "claude-sonnet-4.6": {"cost": {"input": "3 / 1M tokens", "output": "15 / 1M tokens"}},
                "claude-haiku-4.5": {"cost": {"input": 0.25, "output": 1.25}},
            }
        },
        "openai": {
            "models": {
                "gpt-5.5": {"cost": {"input": 5, "output": 20}},
                "gpt-5.4-mini": {"cost": {"input": "0.15 / 1M", "output": "0.6 / 1M"}},
            }
        },
    }
    session = FakeSession({"default": FakeResponse(200, body)})
    cat = ModelsDevCatalog()
    entries = await cat.fetch(session)
    by_model = {(e.provider, e.model): (e.cost_input, e.cost_output) for e in entries}
    assert by_model[("anthropic", "claude-sonnet-4.6")] == (3.0, 15.0)
    assert by_model[("anthropic", "claude-haiku-4.5")] == (0.25, 1.25)
    assert by_model[("openai", "gpt-5.5")] == (5.0, 20.0)
    assert by_model[("openai", "gpt-5.4-mini")] == (0.15, 0.6)


@pytest.mark.asyncio
async def test_models_dev_catalog_handles_missing_pricing():
    body = {
        "openai": {
            "models": {
                "free-model": {},  # no cost field at all
            }
        }
    }
    session = FakeSession({"default": FakeResponse(200, body)})
    cat = ModelsDevCatalog()
    entries = await cat.fetch(session)
    assert entries[0].cost_input is None
    assert entries[0].cost_output is None


# ---------------------------------------------------------------------------
# CatalogClient TTL cache + refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_replaces_entries_on_success():
    """After refresh(), the cached entries reflect the new fetch."""
    body1 = {"models": [{"slug": "model-a"}]}
    body2 = {"models": [{"slug": "model-b"}]}
    session = FakeSession({"default": FakeResponse(200, body1)})

    cat = CodexCatalog()
    await cat.refresh(session)
    assert len(cat._entries) == 1
    assert cat._entries[0].model == "model-a"

    session2 = FakeSession({"default": FakeResponse(200, body2)})
    await cat.refresh(session2)
    assert len(cat._entries) == 1
    assert cat._entries[0].model == "model-b"


@pytest.mark.asyncio
async def test_refresh_keeps_last_good_state_on_failure():
    """A failed refresh doesn't blow away the cached entries."""
    body_ok = {"models": [{"slug": "model-a"}]}
    cat = CodexCatalog()
    session_ok = FakeSession({"default": FakeResponse(200, body_ok)})
    await cat.refresh(session_ok)
    assert cat._last_error is None

    session_fail = FakeSession({"default": FakeResponse(503, {})})
    await cat.refresh(session_fail)
    # Entries retained despite failure
    assert len(cat._entries) == 1
    assert cat._entries[0].model == "model-a"
    assert "503" in (cat._last_error or "")


@pytest.mark.asyncio
async def test_get_returns_cached_until_ttl_expires(monkeypatch):
    """get() should use cached entries when within TTL; only re-fetch on expiry."""
    fetch_count = 0

    class CountingClient(CatalogClient):
        provider = "test"

        async def fetch(self, session):
            nonlocal fetch_count
            fetch_count += 1
            return [CatalogEntry(provider="test", model=f"m-{fetch_count}")]

    # Pin time so we can manipulate TTL deterministically.
    monkeypatch.setattr("tusker_gateway.catalog.time.monotonic", lambda: 1000.0)
    cat = CountingClient()
    cat.ttl_secs = 100.0
    session = FakeSession({})

    await cat.get(session)
    assert fetch_count == 1
    # Within TTL: no re-fetch
    await cat.get(session)
    assert fetch_count == 1
    # Advance past TTL
    monkeypatch.setattr("tusker_gateway.catalog.time.monotonic", lambda: 1200.0)
    await cat.get(session)
    assert fetch_count == 2


# ---------------------------------------------------------------------------
# CatalogRegistry
# ---------------------------------------------------------------------------


def test_registry_default_includes_all_providers():
    reg = CatalogRegistry.default()
    for prov in (
        "openai-codex", "github-copilot", "github-copilot-enterprise",
        "openrouter", "opencode-zen", "opencode-go", "xiaomi", "models.dev",
    ):
        assert reg.get_client(prov) is not None


def test_registry_known_models_returns_set_for_provider():
    reg = CatalogRegistry()
    cat = CodexCatalog()
    cat._entries = [
        CatalogEntry(provider="openai-codex", model="gpt-5.6-sol"),
        CatalogEntry(provider="openai-codex", model="gpt-5.6-luna"),
    ]
    reg.register("openai-codex", cat)
    assert reg.known_models("openai-codex") == {"gpt-5.6-sol", "gpt-5.6-luna"}


def test_registry_known_models_returns_none_for_unknown_provider():
    reg = CatalogRegistry()
    assert reg.known_models("does-not-exist") is None


def test_registry_pricing_lookup_via_models_dev():
    reg = CatalogRegistry()
    md = ModelsDevCatalog()
    md._entries = [
        CatalogEntry(provider="anthropic", model="claude-sonnet-4.6", cost_input=3.0, cost_output=15.0),
    ]
    reg.register("models.dev", md)
    assert reg._pricing_lookup("anthropic", "claude-sonnet-4.6") == (3.0, 15.0)
    assert reg._pricing_lookup("anthropic", "unknown-model") == (None, None)
    assert reg._pricing_lookup("unknown-provider", "claude-sonnet-4.6") == (None, None)


@pytest.mark.asyncio
async def test_registry_refresh_all_calls_every_client():
    fetch_count = 0

    class CountingClient(CatalogClient):
        provider = "test-a"
        async def fetch(self, session):
            nonlocal fetch_count
            fetch_count += 1
            return []
    a = CountingClient()
    a.provider = "test-a"
    b = CountingClient()
    b.provider = "test-b"
    reg = CatalogRegistry()
    reg.register("test-a", a)
    reg.register("test-b", b)
    await reg.refresh_all(FakeSession({}))
    assert fetch_count == 2


# ---------------------------------------------------------------------------
# catalog_refresh_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_loop_stops_when_event_set():
    """The loop exits promptly when stop_event is set."""
    fetch_count = 0
    class CountingClient(CatalogClient):
        provider = "test"
        async def fetch(self, session):
            nonlocal fetch_count
            fetch_count += 1
            return []
    reg = CatalogRegistry()
    reg.register("test", CountingClient())

    stop = asyncio.Event()
    # Long interval so the second refresh never fires
    task = asyncio.create_task(
        catalog_refresh_loop(reg, FakeSession({}), interval_secs=3600.0, stop_event=stop)
    )
    # Give the initial refresh a tick
    await asyncio.sleep(0.05)
    assert fetch_count == 1
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert fetch_count == 1  # No second refresh after stop


# ---------------------------------------------------------------------------
# PoolManager catalog integration
# ---------------------------------------------------------------------------


def test_poolmanager_static_allowlist():
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager
    cfg = {
        "pools": {
            "code": PoolConfig(name="code", models=[
                {"provider": "openai-codex", "model": "gpt-5.6-luna"},
                {"provider": "openrouter", "model": "openai/gpt-oss-20b:free"},
            ]),
        },
        "excluded_providers": [],
        # Bearer-kind providers are dropped from pools without keys.
        "provider_api_keys": {
            "openrouter": "k-openrouter",
            "opencode-zen": "k-zen",
            "opencode-go": "k-go",
        },
        "quality_db_path": "/tmp/_unused.db",
    }
    pm = PoolManager(cfg)
    assert pm.static_allowlist("code") == {
        ("openai-codex", "gpt-5.6-luna"),
        ("openrouter", "openai/gpt-oss-20b:free"),
    }


def test_poolmanager_extend_pools_with_catalog():
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager
    cfg = {
        "pools": {
            "code": PoolConfig(name="code", models=[
                {"provider": "openai-codex", "model": "gpt-5.6-luna"},
                {"provider": "openrouter", "model": "openai/gpt-oss-20b:free"},
                {"provider": "openrouter", "model": "free-model-not-in-catalog"},
            ]),
        },
        "excluded_providers": [],
        # Bearer-kind providers are dropped from pools without keys.
        "provider_api_keys": {
            "openrouter": "k-openrouter",
            "opencode-zen": "k-zen",
            "opencode-go": "k-go",
        },
        "quality_db_path": "/tmp/_unused.db",
    }
    pm = PoolManager(cfg)

    # Build a registry where codex has luna and openrouter has gpt-oss-20b
    reg = CatalogRegistry()
    codex = CodexCatalog()
    codex._entries = [CatalogEntry(provider="openai-codex", model="gpt-5.6-luna")]
    reg.register("openai-codex", codex)
    orr = OpenRouterCatalog()
    orr._entries = [CatalogEntry(provider="openrouter", model="openai/gpt-oss-20b:free")]
    reg.register("openrouter", orr)
    pm.catalog_registry = reg

    confirmed = pm.extend_pools_with_catalog()
    # Both static entries that exist in the catalog are confirmed;
    # "free-model-not-in-catalog" is in static but not in catalog,
    # so it's NOT confirmed by the catalog (still allowed by static).
    assert confirmed["code"] == 2


def test_poolmanager_extend_pools_without_registry():
    """When no registry is set, extend_pools_with_catalog is a no-op."""
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager
    cfg = {
        "pools": {
            "code": PoolConfig(name="code", models=[
                {"provider": "openai-codex", "model": "gpt-5.6-luna"},
            ]),
        },
        "excluded_providers": [],
        # Bearer-kind providers are dropped from pools without keys.
        "provider_api_keys": {
            "openrouter": "k-openrouter",
            "opencode-zen": "k-zen",
            "opencode-go": "k-go",
        },
        "quality_db_path": "/tmp/_unused.db",
    }
    pm = PoolManager(cfg)
    pm.catalog_registry = None
    assert pm.extend_pools_with_catalog() == {}



def test_poolmanager_auto_free_adds_openrouter_zero_pricing():
    """auto_free pulls OpenRouter entries with prompt=0, completion=0
    into the pool's runtime model list."""
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager
    cfg = {
        "pools": {
            "code": PoolConfig(
                name="code",
                models=[{"provider": "openai-codex", "model": "gpt-5.6-luna"}],
                auto_free=True,
            ),
        },
        "excluded_providers": [],
        # Bearer-kind providers are dropped from pools without keys.
        "provider_api_keys": {
            "openrouter": "k-openrouter",
            "opencode-zen": "k-zen",
            "opencode-go": "k-go",
        },
        "quality_db_path": "/tmp/_unused.db",
    }
    pm = PoolManager(cfg)

    reg = CatalogRegistry()
    orr = OpenRouterCatalog()
    orr._entries = [
        # Free entry — should be added.
        CatalogEntry(provider="openrouter", model="stealth/ox-alpha",
                     cost_input=0.0, cost_output=0.0),
        # Paid entry — should NOT be added.
        CatalogEntry(provider="openrouter", model="anthropic/claude-sonnet-4.6",
                     cost_input=3e-6, cost_output=1.5e-5),
        # OpenRouter uses negative pricing sentinels for routers whose
        # underlying model determines the charge — these are not free.
        CatalogEntry(provider="openrouter", model="openrouter/pareto-code",
                     cost_input=-1.0, cost_output=-1.0),
        CatalogEntry(provider="openrouter", model="openrouter/partial-sentinel",
                     cost_input=0.0, cost_output=-1.0),
        # Free catalog entries that are valid upstream products but not
        # general chat backends must not enter a normal chat pool.
        CatalogEntry(provider="openrouter", model="nvidia/nemotron-3.5-content-safety:free",
                     cost_input=0.0, cost_output=0.0),
        CatalogEntry(provider="openrouter", model="openrouter/free",
                     cost_input=0.0, cost_output=0.0),
        # Already-static entry — should stay put, not be re-added.
        CatalogEntry(provider="openai-codex", model="gpt-5.6-luna"),
    ]
    reg.register("openrouter", orr)
    pm.catalog_registry = reg

    pm.extend_pools_with_free_catalog()

    pool_models = {(m["provider"], m["model"]) for m in pm.pools["code"].models}
    assert ("openai-codex", "gpt-5.6-luna") in pool_models  # static kept
    assert ("openrouter", "stealth/ox-alpha") in pool_models  # free added
    assert ("openrouter", "anthropic/claude-sonnet-4.6") not in pool_models  # paid excluded
    assert ("openrouter", "openrouter/pareto-code") not in pool_models
    assert ("openrouter", "openrouter/partial-sentinel") not in pool_models
    assert ("openrouter", "nvidia/nemotron-3.5-content-safety:free") not in pool_models
    assert ("openrouter", "openrouter/free") not in pool_models

    # The runtime model list (self.models) must also reflect the addition
    # so PoolManager.select() can pick it.
    runtime = {(s.provider, s.model) for s in pm.models["code"]}
    assert ("openrouter", "stealth/ox-alpha") in runtime


def test_poolmanager_auto_free_adds_explicitly_free_generic_provider_models():
    """Generic provider catalogs participate only with an explicit 0/0 price."""
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager

    cfg = {
        "pools": {
            "code": PoolConfig(name="code", models=[], auto_free=True),
        },
        "excluded_providers": [],
        "provider_api_keys": {"cerebras": "k-cerebras"},
        "quality_db_path": "/tmp/_unused.db",
    }
    pm = PoolManager(cfg)
    registry = CatalogRegistry()
    client = ProviderModelsCatalog(
        provider="cerebras",
        endpoint="https://api.example.test/v1/models",
    )
    client._entries = [
        CatalogEntry(
            provider="cerebras",
            model="free-chat",
            cost_input=0.0,
            cost_output=0.0,
        ),
        CatalogEntry(
            provider="cerebras",
            model="text-embedding-3-small",
            cost_input=0.0,
            cost_output=0.0,
        ),
        CatalogEntry(
            provider="cerebras",
            model="paid-chat",
            cost_input=0.1,
            cost_output=0.2,
        ),
    ]
    registry.register("cerebras", client)
    pm.catalog_registry = registry

    pm.extend_pools_with_free_catalog()

    assert {(m["provider"], m["model"]) for m in pm.pools["code"].models} == {
        ("cerebras", "free-chat"),
    }


def test_poolmanager_auto_free_does_not_add_non_zdr_catalog_to_privacy():
    """Free pricing is not sufficient to cross the privacy boundary."""
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager

    cfg = {
        "pools": {
            "privacy": PoolConfig(
                name="privacy", models=[], zdr=True, auto_free=True,
            ),
        },
        "excluded_providers": [],
        "provider_api_keys": {"openrouter": "k-openrouter"},
        "quality_db_path": "/tmp/_unused.db",
    }
    pm = PoolManager(cfg)
    registry = CatalogRegistry()
    client = OpenRouterCatalog()
    client._entries = [
        CatalogEntry(
            provider="openrouter",
            model="openai/gpt-oss-20b:free",
            cost_input=0.0,
            cost_output=0.0,
        ),
    ]
    registry.register("openrouter", client)
    pm.catalog_registry = registry

    pm.extend_pools_with_free_catalog()

    assert pm.pools["privacy"].models == []
    assert pm.models["privacy"] == []


def test_poolmanager_auto_free_includes_opencode_zen_and_go():
    """auto_free treats the entire OpenCode Zen/Go catalog as free-for-key,
    since /v1/models is key-filtered (no per-model pricing field)."""
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager
    cfg = {
        "pools": {
            "code": PoolConfig(name="code", models=[], auto_free=True),
        },
        "excluded_providers": [],
        # Bearer-kind providers are dropped from pools without keys.
        "provider_api_keys": {
            "openrouter": "k-openrouter",
            "opencode-zen": "k-zen",
            "opencode-go": "k-go",
        },
        "quality_db_path": "/tmp/_unused.db",
    }
    pm = PoolManager(cfg)

    reg = CatalogRegistry()
    zen = OpenCodeCatalog()
    zen._entries = [
        CatalogEntry(provider="opencode-zen", model="muse-spark-1.2"),
        CatalogEntry(provider="opencode-zen", model="big-pickle"),
    ]
    reg.register("opencode-zen", zen)
    go = OpenCodeGoCatalog()
    go._entries = [
        CatalogEntry(provider="opencode-go", model="minimax-m3"),
        CatalogEntry(provider="opencode-go", model="kimi-k2.6"),
    ]
    reg.register("opencode-go", go)
    pm.catalog_registry = reg

    pm.extend_pools_with_free_catalog()

    pool_models = {(m["provider"], m["model"]) for m in pm.pools["code"].models}
    assert ("opencode-zen", "muse-spark-1.2") in pool_models
    assert ("opencode-zen", "big-pickle") in pool_models
    assert ("opencode-go", "minimax-m3") in pool_models
    assert ("opencode-go", "kimi-k2.6") in pool_models


def test_poolmanager_auto_free_drops_models_that_stop_being_free():
    """Idempotency: when a model goes paid, it must be removed from
    the pool on the next auto_free pass."""
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager
    cfg = {
        "pools": {
            "code": PoolConfig(name="code", models=[], auto_free=True),
        },
        "excluded_providers": [],
        # Bearer-kind providers are dropped from pools without keys.
        "provider_api_keys": {
            "openrouter": "k-openrouter",
            "opencode-zen": "k-zen",
            "opencode-go": "k-go",
        },
        "quality_db_path": "/tmp/_unused.db",
    }
    pm = PoolManager(cfg)

    # First pass: stealth/ox-alpha is free, gets added.
    reg1 = CatalogRegistry()
    orr1 = OpenRouterCatalog()
    orr1._entries = [CatalogEntry(provider="openrouter", model="stealth/ox-alpha",
                                  cost_input=0.0, cost_output=0.0)]
    reg1.register("openrouter", orr1)
    pm.catalog_registry = reg1
    pm.extend_pools_with_free_catalog()
    assert ("openrouter", "stealth/ox-alpha") in {(m["provider"], m["model"]) for m in pm.pools["code"].models}

    # Second pass: stealth/ox-alpha is now paid (cost > 0).
    reg2 = CatalogRegistry()
    orr2 = OpenRouterCatalog()
    orr2._entries = [CatalogEntry(provider="openrouter", model="stealth/ox-alpha",
                                  cost_input=3e-6, cost_output=1.5e-5)]
    reg2.register("openrouter", orr2)
    pm.catalog_registry = reg2
    pm.extend_pools_with_free_catalog()
    assert ("openrouter", "stealth/ox-alpha") not in {(m["provider"], m["model"]) for m in pm.pools["code"].models}


def test_poolmanager_auto_free_disabled_is_noop():
    """auto_free=False (default) means the catalog is ignored entirely."""
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager
    cfg = {
        "pools": {
            "code": PoolConfig(name="code", models=[], auto_free=False),
        },
        "excluded_providers": [],
        # Bearer-kind providers are dropped from pools without keys.
        "provider_api_keys": {
            "openrouter": "k-openrouter",
            "opencode-zen": "k-zen",
            "opencode-go": "k-go",
        },
        "quality_db_path": "/tmp/_unused.db",
    }
    pm = PoolManager(cfg)

    reg = CatalogRegistry()
    orr = OpenRouterCatalog()
    orr._entries = [CatalogEntry(provider="openrouter", model="stealth/ox-alpha",
                                 cost_input=0.0, cost_output=0.0)]
    reg.register("openrouter", orr)
    pm.catalog_registry = reg

    pm.extend_pools_with_free_catalog()
    assert pm.pools["code"].models == []
