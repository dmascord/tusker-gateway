"""Admin protocol-probe service: authorization, executor binding, evidence.

No network access happens in these tests: executors are monkeypatched and
the aiohttp layer is never opened.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_probe_service import (
    PROBES_ENABLED_ENV,
    ProtocolProbeError,
    probes_enabled,
    probe_target_from_request,
)


def _config() -> dict[str, Any]:
    return {
        "providers": {
            "minimax": {
                "base_url": "https://api.minimax.io",
                "chat_path": "/v1/chat/completions",
                "auth_type": "bearer",
                "model_aliases": {"MiniMax-M2": "MiniMax-M2"},
            },
        },
        "models": {
            "pools": {
                "code": {"models": [
                    {"provider": "minimax", "model": "MiniMax-M2"},
                ]},
            },
        },
        "api_keys": ["sk-test"],
        "quality_db_path": "/tmp/test-quality.db",
    }


def test_probe_target_requires_configured_provider():
    cfg = _config()
    provider, model, endpoint, endpoint_id = probe_target_from_request(
        cfg, {"provider": "minimax", "model": "MiniMax-M2"},
    )
    assert provider == "minimax"
    assert endpoint["base_url"] == "https://api.minimax.io"
    assert endpoint_id
    with pytest.raises(ProtocolProbeError) as exc:
        probe_target_from_request(cfg, {"provider": "nope", "model": "x"})
    assert exc.value.status == 404


def test_probe_target_rejects_unknown_model():
    with pytest.raises(ProtocolProbeError) as exc:
        probe_target_from_request(
            _config(), {"provider": "minimax", "model": "arbitrary-model"},
        )
    assert exc.value.status == 404


def test_probes_disabled_by_default(monkeypatch):
    monkeypatch.delenv(PROBES_ENABLED_ENV, raising=False)
    assert probes_enabled() is False
    monkeypatch.setenv(PROBES_ENABLED_ENV, "1")
    assert probes_enabled() is True


def test_run_probe_requires_approval_and_gate(monkeypatch):
    from tusker_gateway.protocol_probe_service import run_probe

    monkeypatch.delenv(PROBES_ENABLED_ENV, raising=False)
    app = {"config": _config()}
    with pytest.raises(ProtocolProbeError) as exc:
        asyncio.run(run_probe(app, {"provider": "minimax", "model": "MiniMax-M2",
                                    "approved": True}))
    assert exc.value.code == "probes_disabled"

    monkeypatch.setenv(PROBES_ENABLED_ENV, "1")
    with pytest.raises(ProtocolProbeError) as exc:
        asyncio.run(run_probe(app, {"provider": "minimax", "model": "MiniMax-M2",
                                    "approved": False}))
    assert exc.value.code == "approval_required"


def test_run_probe_executes_bound_executor_and_records_evidence(
    monkeypatch, tmp_path,
):
    """Happy path: probes run once, evidence recorded, no bodies returned."""
    from tusker_gateway.protocol_probe import ProbeAssessment
    from tusker_gateway.protocol_probe_service import run_probe

    monkeypatch.setenv(PROBES_ENABLED_ENV, "1")
    monkeypatch.setenv("TUSKER_PROTOCOL_EVIDENCE_DB_PATH", str(tmp_path / "ev.db"))

    captured: dict[str, Any] = {}

    class _FakeExecutor:
        def __init__(self, *, url, headers, session, model_header, upstream_model):
            captured["url"] = url
            captured["headers"] = headers
            captured["upstream_model"] = upstream_model

        async def __call__(self, request):
            captured["request"] = request
            return {"choices": [{"finish_reason": "tool_calls", "message": {
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "protocol_probe_echo",
                                             "arguments": '{"value":"ok"}'}}]}}]}

        async def chat_sse(self, request):
            async def _it():
                yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"protocol_probe_echo","arguments":"{\\"value\\":\\"ok\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'
                yield b"data: [DONE]\n\n"
            return _it()

    import tusker_gateway.protocol_probe_service as service

    monkeypatch.setattr(service, "_BoundExecutor", _FakeExecutor)

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def _build_request(self, *a, **k):
            return {"Authorization": "Bearer test"}, {}

    class _FakeQuality:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(service, "PassthroughClient", _FakeClient, raising=False)
    monkeypatch.setattr(
        service, "QualityDB", _FakeQuality, raising=False,
    )
    # The service imports these inside run_probe; patch the source modules.
    import tusker_gateway.passthrough as passthrough_module
    import tusker_gateway.quality as quality_module

    monkeypatch.setattr(passthrough_module, "PassthroughClient", _FakeClient)
    monkeypatch.setattr(quality_module, "QualityDB", _FakeQuality, raising=False)

    app = {
        "config": _config(),
        "http_session": object(),
        "protocol_probe_coordinator": None,
    }
    result = asyncio.run(run_probe(app, {
        "provider": "minimax", "model": "MiniMax-M2", "approved": True,
        "features": ["structured_tool_calls"],
        "timeout_seconds": 5,
    }))
    assert captured["url"] == "https://api.minimax.io/v1/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer test"}
    assert captured["request"]["tools"][0]["function"]["name"] == "protocol_probe_echo"
    assert result["results"]["structured_tool_calls"]["status"] == "passed"
    # Evidence recorded for the endpoint identity, and nothing else leaks.
    store = ProtocolEvidenceStore(str(tmp_path / "ev.db"))
    assert store.get("minimax", result["endpoint_id"], "MiniMax-M2", "structured_tool_calls").verified
    assert {"structured_tool_calls", "tool_call_finish", "reasoning_channel"} <= set(result["results"].keys())


def test_cooldown_blocks_second_probe(monkeypatch, tmp_path):
    """The coordinator enforces per-target cooldown across runs."""
    from tusker_gateway.protocol_probe_coordinator import ProbeCoordinator
    from tusker_gateway.protocol_probe_service import ProtocolProbeError, run_probe

    monkeypatch.setenv(PROBES_ENABLED_ENV, "1")
    coordinator = ProbeCoordinator(max_concurrent=1, cooldown_seconds=300)
    ran = {"n": 0}

    class _StubExecutor:
        def __init__(self, **kwargs):
            pass

        async def __call__(self, request):
            ran["n"] += 1
            return {"choices": [{"finish_reason": "stop", "message": {
                "content": "ok", "tool_calls": [],}}]}

        async def chat_sse(self, request):
            async def _it():
                yield b"data: [DONE]\n\n"
            return _it()

    import tusker_gateway.protocol_probe_service as service
    monkeypatch.setattr(service, "_BoundExecutor", _StubExecutor)

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def _build_request(self, *a, **k):
            return {}, {}

    import tusker_gateway.passthrough as passthrough_module
    monkeypatch.setattr(passthrough_module, "PassthroughClient", _FakeClient)

    app = {
        "config": _config(),
        "http_session": object(),
        "protocol_probe_coordinator": coordinator,
    }
    payload = {
        "provider": "minimax", "model": "MiniMax-M2", "approved": True,
        "features": ["structured_tool_calls"], "timeout_seconds": 5,
    }
    asyncio.run(run_probe(app, payload))
    assert ran["n"] == 1
    with pytest.raises(ProtocolProbeError) as exc:
        asyncio.run(run_probe(app, payload))
    assert exc.value.code == "cooldown_active"
    assert ran["n"] == 1


def test_executor_maps_5xx_to_transient(monkeypatch):
    """5xx responses must classify as unavailable, never unsupported."""
    from tusker_gateway.protocol_probe_service import _BoundExecutor

    class _Resp:
        status = 503
        async def json(self, content_type=None):
            return {"error": "boom"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    class _Session:
        async def post(self, *a, **k):
            return _Resp()

    executor = _BoundExecutor(
        url="https://x/v1/chat/completions", headers={}, session=_Session(),
        model_header=None, upstream_model="m",
    )
    with pytest.raises(ConnectionError):
        asyncio.run(executor({"model": "m", "messages": []}))


def test_evidence_snapshot_shape(tmp_path, monkeypatch):
    from tusker_gateway.protocol_probe_service import evidence_snapshot

    monkeypatch.setenv("TUSKER_PROTOCOL_EVIDENCE_DB_PATH", str(tmp_path / "ev.db"))
    store = ProtocolEvidenceStore(str(tmp_path / "ev.db"))
    store.record("minimax", "ep1", "MiniMax-M2", "structured_tool_calls", "passed")
    snap = evidence_snapshot(_config())
    assert snap["probe_version"]
    rows = [r for r in snap["evidence"] if r["model"] == "MiniMax-M2"]
    assert rows and rows[0]["status"] == "passed" and rows[0]["verified"] is True
    # No payload fields anywhere in the snapshot.
    assert all("payload" not in r and "body" not in r for r in snap["evidence"])
