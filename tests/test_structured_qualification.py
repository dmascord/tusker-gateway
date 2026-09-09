"""Tests for the Hindsight structured-output qualification probe."""
from __future__ import annotations

import json
import time
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tusker_gateway.catalog import CatalogRegistry, OpenRouterCatalog
from tusker_gateway.config import PoolConfig
from tusker_gateway.cooldown import global_tracker
from tusker_gateway.maintenance import run_structured_maintenance_cycle
from tusker_gateway.persistent_cooldown import PersistentCooldownStore
from tusker_gateway.pools import PoolManager

from tusker_gateway.model_capability import STRUCTURED_OUTPUT_PROBE_VERSION
from tusker_gateway.structured_qualification import (
    _classify_http_failure,
    probe_model,
    qualified_count,
    run_structured_qualification,
)


class _Response:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self) -> bytes:
        if isinstance(self._body, bytes):
            return self._body
        return json.dumps(self._body).encode()

    async def json(self, **kwargs: Any) -> Any:
        return self._body


class _Session:
    def __init__(self, response: _Response):
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_probe_model_accepts_exact_json_contract_without_retaining_body():
    session = _Session(_Response(200, {
        "choices": [{"message": {"content": '{"ok": true}'}}],
    }))

    result = await probe_model(
        session,
        base_url="http://gateway.test",
        api_key="gateway-key",
        provider="synthetic",
        model="syn:small:text",
    )

    assert result["status"] == "passed"
    assert result["capability"] == "structured_output"
    assert result["probe_version"] == STRUCTURED_OUTPUT_PROBE_VERSION
    assert result["http_status"] == 200
    assert "body" not in result
    assert "response" not in result
    assert session.calls[0][0] == "http://gateway.test/v1/chat/completions"
    payload = session.calls[0][1]["json"]
    assert payload["model"] == "synthetic::syn:small:text"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_probe_model_rejects_malformed_json_as_unsupported():
    session = _Session(_Response(200, {
        "choices": [{"message": {"content": "Here is the JSON: {}"}}],
    }))

    result = await probe_model(
        session,
        base_url="http://gateway.test",
        api_key="gateway-key",
        provider="openrouter",
        model="model",
    )

    assert result["status"] == "unsupported"
    assert result["failure_class"] == "invalid_json"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        '{"ok": 1}',            # numeric 1 == True in Python, but not a boolean
        '{"ok": "true"}',       # string, not a boolean
        '{"ok": true, "extra": 1}',  # additional property
        '{"ok": false}',        # boolean false is not a pass
        '[]',                   # wrong JSON type (list)
    ],
)
async def test_probe_model_rejects_non_boolean_or_extra_key_json(content):
    session = _Session(_Response(200, {
        "choices": [{"message": {"content": content}}],
    }))

    result = await probe_model(
        session,
        base_url="http://gateway.test",
        api_key="gateway-key",
        provider="openrouter",
        model="model",
    )

    assert result["status"] == "unsupported"
    assert result["failure_class"] == "schema_mismatch"


def test_structured_failure_classification_keeps_transient_errors_retryable():
    assert _classify_http_failure(400, "response_format is not supported") == (
        "unsupported",
        "structured_output_rejected",
    )
    assert _classify_http_failure(429, "rate limit exceeded") == (
        "unavailable",
        "rate_limited",
    )
    assert _classify_http_failure(
        502,
        "upstream failed",
        {"X-Tusker-Provider-Failure": "provider_quota"},
    ) == ("unavailable", "provider_quota")


def _structured_config(tmp_path, models):
    return {
        "pools": {"privacy": PoolConfig(name="privacy", zdr=True, models=models)},
        "quality_db_path": str(tmp_path / "quality.db"),
        "model_capability_db_path": str(tmp_path / "model-capability.db"),
        "excluded_providers": [],
        "provider_api_keys": {"synthetic": "k-synthetic"},
        "providers": {"synthetic": {"zdr_ok": True, "kind": "bearer"}},
    }


def _record_pass(manager, model, **kwargs):
    manager._model_capability_db.record(
        provider="synthetic", model=model, capability="structured_output",
        status="passed", source="structured_probe",
        probe_version=STRUCTURED_OUTPUT_PROBE_VERSION, **kwargs,
    )


async def _start_loopback(probed):
    async def handler(request):
        payload = await request.json()
        probed.append(payload["model"])
        return web.json_response({
            "choices": [{"message": {"content": '{"ok": true}'}}],
        })

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.mark.asyncio
async def test_successive_cycles_probe_unseen_then_oldest(monkeypatch, tmp_path):
    manager = PoolManager(_structured_config(tmp_path, [
        {"provider": "synthetic", "model": model}
        for model in ("a-stale", "b-new", "c-new")
    ]))
    _record_pass(manager, "a-stale", checked_at=time.time() - 21_601)
    monkeypatch.setenv("API_KEYS", "gateway-key")
    probed = []
    server = await _start_loopback(probed)
    try:
        for _ in range(3):
            await run_structured_qualification(
                manager=manager, base_url=str(server.make_url("")), limit=1,
                max_age_secs=0, catalog_registry=CatalogRegistry(),
            )
        assert probed == ["synthetic::b-new", "synthetic::c-new", "synthetic::a-stale"]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_quarantined_routes_send_no_further_probe_http(monkeypatch, tmp_path):
    manager = PoolManager(_structured_config(tmp_path, [
        {"provider": "synthetic", "model": "chat"},
    ]))
    monkeypatch.setenv("API_KEYS", "gateway-key")
    probed = []
    server = await _start_loopback(probed)
    try:
        async def run():
            return await run_structured_qualification(
                manager=manager, base_url=str(server.make_url("")), force=True,
                catalog_registry=CatalogRegistry(),
            )

        await run()
        PersistentCooldownStore(tmp_path / "cooldowns.db").record("synthetic", "chat", 3600)
        assert await run() == []
        assert probed == ["synthetic::chat"]
    finally:
        await server.close()


@pytest.mark.parametrize("persistent", [False, True])
def test_qualified_count_zero_when_all_fresh_passes_cooled(tmp_path, persistent):
    manager = PoolManager(_structured_config(tmp_path, [
        {"provider": "synthetic", "model": "chat"},
    ]))
    _record_pass(manager, "chat")
    assert qualified_count(manager=manager) == 1
    if persistent:
        PersistentCooldownStore(tmp_path / "cooldowns.db").record("synthetic", "chat", 3600)
    else:
        global_tracker().cooldown("synthetic", "chat", 3600)
    assert qualified_count(manager=manager) == 0


def test_qualified_count_excludes_heavyweight_and_zdr_blocked_passes(tmp_path):
    config = _structured_config(tmp_path, [
        {"provider": "synthetic", "model": "chat"},
        {"provider": "synthetic", "model": "heavy", "heavyweight": True},
        {"provider": "unsafe", "model": "chat"},
    ])
    config["providers"]["unsafe"] = {"kind": "bearer", "zdr_ok": False}
    config["provider_api_keys"]["unsafe"] = "key"
    manager = PoolManager(config)
    _record_pass(manager, "chat")
    _record_pass(manager, "heavy")
    manager._model_capability_db.record(
        provider="unsafe", model="chat", capability="structured_output",
        status="passed", source="structured_probe",
        probe_version=STRUCTURED_OUTPUT_PROBE_VERSION,
    )
    assert qualified_count(manager=manager) == 1


def test_qualified_count_preserves_runtime_rotation(tmp_path):
    manager = PoolManager(_structured_config(tmp_path, [
        {"provider": "synthetic", "model": model} for model in ("a", "b")
    ]))
    for model in ("a", "b"):
        _record_pass(manager, model)
    assert manager.select("privacy") == ("synthetic", "a")
    assert qualified_count(manager=manager) == 2
    assert manager.select("privacy") == ("synthetic", "b")


def test_qualified_count_ignores_stale_and_old_probe_passes(tmp_path):
    manager = PoolManager(_structured_config(tmp_path, [
        {"provider": "synthetic", "model": model} for model in ("stale", "old", "unknown")
    ]))
    _record_pass(manager, "stale", checked_at=time.time() - 21_601)
    manager._model_capability_db.record(
        provider="synthetic", model="old", capability="structured_output",
        status="passed", source="structured_probe", probe_version="structured-output-v1",
    )
    assert qualified_count(manager=manager) == 0
    assert manager.select("privacy", requires_structured_output=True) is not None


@pytest.mark.asyncio
async def test_maintenance_probes_and_counts_catalog_added_models(monkeypatch, tmp_path):
    config = {
        "pools": {"code": PoolConfig(name="code", models=[], auto_free=True)},
        "quality_db_path": str(tmp_path / "quality.db"),
        "provider_api_keys": {"openrouter": "key"},
        "providers": {"openrouter": {"kind": "bearer"}},
    }
    catalog_requests = []
    probed = []

    async def catalog(request):
        catalog_requests.append(request.path)
        return web.json_response({"data": [
            {"id": "free-chat", "pricing": {"prompt": "0", "completion": "0"}},
        ]})

    async def chat(request):
        probed.append((await request.json())["model"])
        return web.json_response({"choices": [{"message": {"content": '{"ok":true}'}}]})

    app = web.Application()
    app.router.add_get("/models", catalog)
    app.router.add_post("/v1/chat/completions", chat)
    async with TestServer(app) as server:
        client = OpenRouterCatalog()
        client.ENDPOINT = str(server.make_url("/models"))
        registry = CatalogRegistry({"openrouter": client})
        monkeypatch.setenv("API_KEYS", "gateway-key")
        monkeypatch.setattr("tusker_gateway.maintenance.load_config", lambda: config)
        monkeypatch.setattr(
            "tusker_gateway.tool_qualification._catalog_registry",
            lambda *args, **kwargs: registry,
        )
        summary = await run_structured_maintenance_cycle(
            pool_name="code", base_url=str(server.make_url("")),
        )
        assert probed == ["openrouter::free-chat"]
        assert catalog_requests == ["/models"]
        assert summary["qualified"] == 1
        assert summary["passed"] == 1
@pytest.mark.asyncio
async def test_structured_probes_skip_non_text_output_catalog_models(monkeypatch, tmp_path):
    """A catalog model advertising a non-text output modality (e.g. TTS) is
    never probed: the chat-shaped probe could only fail and poison stats."""
    config = {
        "pools": {"code": PoolConfig(name="code", models=[], auto_free=True)},
        "quality_db_path": str(tmp_path / "quality.db"),
        "provider_api_keys": {"openrouter": "key"},
        "providers": {"openrouter": {"kind": "bearer"}},
    }
    probed = []

    async def catalog(request):
        return web.json_response({"data": [
            {"id": "free-chat", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "tts-model",
             "pricing": {"prompt": "0", "completion": "0"},
             "output_modalities": ["speech"]},
        ]})

    async def chat(request):
        probed.append((await request.json())["model"])
        return web.json_response({"choices": [{"message": {"content": '{"ok":true}'}}]})

    app = web.Application()
    app.router.add_get("/models", catalog)
    app.router.add_post("/v1/chat/completions", chat)
    async with TestServer(app) as server:
        client = OpenRouterCatalog()
        client.ENDPOINT = str(server.make_url("/models"))
        registry = CatalogRegistry({"openrouter": client})
        monkeypatch.setenv("API_KEYS", "gateway-key")
        monkeypatch.setattr(
            "tusker_gateway.structured_qualification.load_config",
            lambda: config,
        )
        monkeypatch.setattr(
            "tusker_gateway.tool_qualification._catalog_registry",
            lambda *args, **kwargs: registry,
        )
        summary = await run_structured_qualification(
            pool_name="code", base_url=str(server.make_url("")),
        )
        assert probed == ["openrouter::free-chat"]
        assert all("tts-model" not in r for r in summary)
