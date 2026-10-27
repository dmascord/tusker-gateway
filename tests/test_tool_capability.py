"""Tests for behavioral tool-call qualification and pool gating."""
from __future__ import annotations

import asyncio
import os
import pytest

from tusker_gateway.config import PoolConfig
from tusker_gateway.pools import PoolManager
from tusker_gateway.tool_capability import (
    TOOL_CAPABILITY_PROBE_VERSION,
    ToolCapabilityDB,
    ToolCapabilityLevel,
)
from tusker_gateway.tool_qualification import (
    COMPOSITE_CAPABILITY,
    COMPOSITE_CAPABILITY_PROBE_VERSION,
    PROBE_PATH,
    PROBE_TOOL_NAME,
    _classify_http_failure,
    _is_account_quota_exhausted,
    _needs_probe,
    _route_is_quarantined,
    _result_from_stream,
    probe_image_tool_model,
    probe_model,
)


class _Entry:
    provider = "openrouter"
    model = "new-model:free"
    cost_input = 0.0
    cost_output = 0.0
    input_modalities = frozenset({"text"})
    raw = {"supported_parameters": ["tools"]}


class _Registry:
    def entries_for(self, provider: str):
        return [_Entry()] if provider == "openrouter" else []


def _manager(tmp_path):
    return PoolManager(
        {
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[],
                    auto_catalog=True,
                ),
            },
            "quality_db_path": os.path.join(tmp_path, "quality.db"),
            "tool_capability_db_path": os.path.join(tmp_path, "capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openrouter": "test-key"},
        }
    )


class _ProbeResponse:
    status = 200
    headers: dict[str, str] = {}
    content: "_ProbeResponse"

    def __init__(self):
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _ProbeSession:
    def __init__(self):
        self.headers: dict[str, str] | None = None
        self.payload: dict | None = None

    def post(self, _url, **kwargs):
        self.headers = kwargs["headers"]
        self.payload = kwargs["json"]
        return _ProbeResponse()
class _CompositeResponse(_ProbeResponse):
    def __init__(self, lines):
        super().__init__()
        self._lines = lines

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _CompositeSession:
    def __init__(self, response):
        self.response = response
        self.payload = None

    def post(self, _url, **kwargs):
        self.payload = kwargs["json"]
        return self.response


def _sse(event):
    return ("data: " + __import__("json").dumps(event) + "\n").encode()


@pytest.mark.asyncio
async def test_composite_probe_passes_image_and_exact_tool_call():
    response = _CompositeResponse([
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": PROBE_TOOL_NAME, "arguments": '{"path":"/tmp/tusker-tool-capability-probe"}'}}]}, "finish_reason": "tool_calls"}]}),
    ])
    session = _CompositeSession(response)
    result = await probe_image_tool_model(session, base_url="http://gateway.test", api_key="key", provider="openrouter", model="vision")
    assert result["status"] == "passed"
    assert result["capability"] == COMPOSITE_CAPABILITY
    assert session.payload["messages"][1]["content"][1]["type"] == "image_url"
    assert session.payload["tools"]

@pytest.mark.asyncio
async def test_composite_probe_allows_reasoning_budget_for_multimodal_models():
    response = _CompositeResponse([
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": PROBE_TOOL_NAME, "arguments": '{"path":"/tmp/tusker-tool-capability-probe"}'}}]}, "finish_reason": "tool_calls"}]}),
    ])
    session = _CompositeSession(response)
    result = await probe_image_tool_model(
        session, base_url="http://gateway.test", api_key="key",
        provider="ollama-cloud", model="glm-5.3-flash",
    )
    assert result["status"] == "passed"
    assert session.payload["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_composite_probe_classifies_missing_tool_result_as_unsupported():
    response = _CompositeResponse([_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})])
    result = await probe_image_tool_model(_CompositeSession(response), base_url="http://gateway.test", api_key="key", provider="openrouter", model="vision")
    assert result["status"] == "unsupported"
    assert result["failure_class"] == "no_tool_call"


@pytest.mark.asyncio
async def test_composite_probe_classifies_malformed_tool_result_as_unsupported():
    response = _CompositeResponse([
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": PROBE_TOOL_NAME, "arguments": "not-json"}}]}, "finish_reason": "tool_calls"}]}),
    ])
    result = await probe_image_tool_model(
        _CompositeSession(response), base_url="http://gateway.test", api_key="key",
        provider="openrouter", model="vision",
    )
    assert result["status"] == "unsupported"
    assert result["failure_class"] == "non_strict_tool_contract"


def _stream_error_frame(code: str) -> bytes:
    return _sse({"error": {"error": {
        "type": "invalid_request_error",
        "message": f"gateway {code}",
        "code": code,
    }}})


@pytest.mark.asyncio
async def test_probe_model_treats_disabled_route_as_transient():
    # github-copilot is disabled: the gateway answers HTTP 200 with an inline
    # provider_route_disabled frame and no tool call. That is an availability
    # state, not a capability verdict, so it must not become a permanent
    # no_tool_call failure.
    response = _CompositeResponse([
        _stream_error_frame("provider_route_disabled"),
        b"data: [DONE]\n\n",
    ])
    result = await probe_model(
        _CompositeSession(response),
        base_url="http://gateway.test", api_key="key",
        provider="github-copilot", model="gpt-4o",
    )
    assert result["status"] == "unavailable"
    assert result["level"] == ToolCapabilityLevel.UNAVAILABLE
    assert result["failure_class"] == "route_disabled"
    assert result["tool_call_count"] == 0


@pytest.mark.asyncio
async def test_probe_model_treats_circuit_open_as_transient():
    response = _CompositeResponse([
        _stream_error_frame("circuit_open"),
        b"data: [DONE]\n\n",
    ])
    result = await probe_model(
        _CompositeSession(response),
        base_url="http://gateway.test", api_key="key",
        provider="github-copilot-enterprise", model="gpt-5.6-luna",
    )
    assert result["status"] == "unavailable"
    assert result["level"] == ToolCapabilityLevel.UNAVAILABLE
    assert result["failure_class"] == "circuit_open"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "failure_class"),
    [
        ("model_blacklisted", "route_blacklisted"),
        ("provider_empty_stream", "provider_empty_stream"),
        ("stream_validation_error", "stream_validation_error"),
        ("approval_required", "approval_required"),
    ],
)
async def test_probe_model_maps_remaining_gateway_error_codes(code, failure_class):
    response = _CompositeResponse([
        _stream_error_frame(code),
        b"data: [DONE]\n\n",
    ])
    result = await probe_model(
        _CompositeSession(response),
        base_url="http://gateway.test", api_key="key",
        provider="ollama-cloud", model="kimi-k3",
    )
    assert result["status"] == "unavailable"
    assert result["level"] == ToolCapabilityLevel.UNAVAILABLE
    assert result["failure_class"] == failure_class


@pytest.mark.asyncio
async def test_probe_model_keeps_genuine_no_tool_call_as_permanent():
    # A real completion that simply declines to call the tool (finish_reason
    # stop, no error frame) is a behavioral failure and stays permanent.
    response = _CompositeResponse([
        _sse({"choices": [{"delta": {"content": "I cannot."}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n\n",
    ])
    result = await probe_model(
        _CompositeSession(response),
        base_url="http://gateway.test", api_key="key",
        provider="openrouter", model="prose-only",
    )
    assert result["status"] == "failed"
    assert result["level"] == ToolCapabilityLevel.UNSUPPORTED
    assert result["failure_class"] == "no_tool_call"


@pytest.mark.asyncio
async def test_composite_probe_treats_circuit_open_as_transient():
    response = _CompositeResponse([
        _stream_error_frame("circuit_open"),
        b"data: [DONE]\n\n",
    ])
    result = await probe_image_tool_model(
        _CompositeSession(response), base_url="http://gateway.test", api_key="key",
        provider="github-copilot-enterprise", model="gpt-5.6-luna",
    )
    assert result["status"] == "unavailable"
    assert result["failure_class"] == "circuit_open"


@pytest.mark.asyncio
async def test_probe_model_prefers_tool_call_over_trailing_error():
    # A valid tool call followed by an error frame is still a passing probe:
    # the transient downgrade only applies when no tool call was captured.
    response = _CompositeResponse([
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": PROBE_TOOL_NAME, "arguments": '{"path":"/tmp/tusker-tool-capability-probe"}'}}]}, "finish_reason": "tool_calls"}]}),
        _stream_error_frame("provider_error"),
        b"data: [DONE]\n\n",
    ])
    result = await probe_model(
        _CompositeSession(response),
        base_url="http://gateway.test", api_key="key",
        provider="openrouter", model="good",
    )
    assert result["status"] == "passed"
    assert result["failure_class"] is None


@pytest.mark.asyncio
async def test_probe_model_treats_post_commit_marker_as_transient():
    response = _CompositeResponse([
        _sse({
            "choices": [{
                "delta": {"content": "Upstream provider error: quota exceeded."},
                "finish_reason": "stop",
            }],
            "x_tusker_stream_error": "provider_error",
        }),
        b"data: [DONE]\n\n",
    ])
    result = await probe_model(
        _CompositeSession(response),
        base_url="http://gateway.test", api_key="key",
        provider="github-copilot-enterprise", model="gpt-5.4",
    )
    assert result["status"] == "unavailable"
    assert result["level"] == ToolCapabilityLevel.UNAVAILABLE
    assert result["failure_class"] == "provider_error"


def test_opencode_probe_sends_isolated_session_header():
    session = _ProbeSession()

    asyncio.run(
        probe_model(
            session,
            base_url="https://opencode.test",
            api_key="test-key",
            provider="opencode-zen",
            model="test-model",
        )
    )

    assert session.headers is not None
    assert session.headers["X-Opencode-Session"].startswith("probe-")

def test_composite_runner_refreshes_stale_or_unavailable_records(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tusker_gateway.model_capability import ModelCapabilityDB
    from tusker_gateway.tool_qualification import run_image_tool_qualification

    db = ModelCapabilityDB(str(tmp_path / "model-capability.db"))
    db.record(
        provider="ollama-cloud", model="fresh", capability=COMPOSITE_CAPABILITY,
        status="passed", source="composite_probe",
        probe_version=COMPOSITE_CAPABILITY_PROBE_VERSION,
    )
    db.record(
        provider="ollama-cloud", model="unavailable", capability=COMPOSITE_CAPABILITY,
        status="unavailable", source="composite_probe",
        probe_version=COMPOSITE_CAPABILITY_PROBE_VERSION,
    )
    config = {
        "quality_db_path": str(tmp_path / "quality.db"),
        "model_capability_db_path": str(tmp_path / "model-capability.db"),
        "pools": {},
    }
    monkeypatch.setattr("tusker_gateway.tool_qualification.load_config", lambda: config)
    monkeypatch.setenv("API_KEYS", "test-key")
    # The direct runner is exercised through its explicit-pair path; the
    # freshness predicate is checked by replacing the network probe.
    async def fake_probe(*args, **kwargs):
        return {"provider": kwargs["provider"], "model": kwargs["model"], "status": "passed"}
    monkeypatch.setattr("tusker_gateway.tool_qualification.probe_image_tool_model", fake_probe)

    async def run():
        return await run_image_tool_qualification(
            base_url="http://gateway.test", model_pairs={
                ("ollama-cloud", "fresh"), ("ollama-cloud", "unavailable")
            }, max_age_secs=86_400.0,
        )

    results = asyncio.run(run())
    assert [item["model"] for item in results] == ["unavailable"]


def test_non_opencode_probe_omits_session_header():
    session = _ProbeSession()

    asyncio.run(
        probe_model(
            session,
            base_url="https://provider.test",
            api_key="test-key",
            provider="openrouter",
            model="test-model",
        )
    )

    assert session.headers is not None
    assert "X-Opencode-Session" not in session.headers


def test_apim_gpt6_probe_exercises_reasoning_compatibility_adapter():
    session = _ProbeSession()

    asyncio.run(
        probe_model(
            session,
            base_url="https://apim.test",
            api_key="test-key",
            provider="apim",
            model="gpt-6-luna",
        )
    )

    assert session.payload is not None
    assert session.payload["reasoning_effort"] == "high"


def test_other_provider_probe_does_not_add_reasoning_effort():
    session = _ProbeSession()

    asyncio.run(
        probe_model(
            session,
            base_url="https://provider.test",
            api_key="test-key",
            provider="openrouter",
            model="test-model",
        )
    )

    assert session.payload is not None
    assert "reasoning_effort" not in session.payload


def test_tool_capability_db_round_trip_and_gate(tmp_path):
    db = ToolCapabilityDB(str(tmp_path / "capability.db"))
    result = db.record(
        provider="openrouter",
        model="good",
        level=ToolCapabilityLevel.STRICT_STRUCTURED_STREAM,
        status="passed",
        http_status=200,
        tool_call_count=1,
        structured_stream=True,
        arguments_valid=True,
        arguments_match=True,
        finish_reason="tool_calls",
        checked_at=100.0,
    )

    assert result.qualified_for_tools is True
    assert db.is_qualified("openrouter", "good") is True
    assert db.status()["qualified_models"] == 1

    db.record(
        provider="openrouter",
        model="bad",
        level=ToolCapabilityLevel.UNSUPPORTED,
        status="failed",
        failure_class="no_tool_call",
    )
    assert db.is_qualified("openrouter", "bad") is False


def test_tool_selection_reads_capability_table_once(tmp_path, monkeypatch):
    manager = PoolManager(
        {
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[
                        {"provider": "openrouter", "model": "curated-a"},
                        {"provider": "openrouter", "model": "curated-b"},
                    ],
                ),
            },
            "quality_db_path": os.path.join(tmp_path, "quality.db"),
            "tool_capability_db_path": os.path.join(tmp_path, "capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openrouter": "test-key"},
        }
    )
    records_calls = 0

    original_records = manager._tool_capabilities.records

    def records_once():
        nonlocal records_calls
        records_calls += 1
        return original_records()

    monkeypatch.setattr(manager._tool_capabilities, "records", records_once)
    monkeypatch.setattr(
        manager._tool_capabilities,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("selection should use its snapshot")
        ),
    )

    assert manager.select("code", requires_tools=True) == (
        "openrouter",
        "curated-a",
    )
    assert records_calls == 1


def test_auto_discovered_model_requires_qualified_stream_probe(tmp_path):
    manager = _manager(str(tmp_path))
    manager.catalog_registry = _Registry()
    manager.extend_pools_with_auto_catalog()

    assert manager.auto_added["code"] == {("openrouter", "new-model:free")}
    assert manager.select("code", requires_tools=True) is None

    assert manager.select("privacy") is None

    manager._tool_capabilities.record(
        provider="openrouter",
        model="new-model:free",
        level=ToolCapabilityLevel.STRICT_STRUCTURED_STREAM,
        status="passed",
        http_status=200,
        tool_call_count=1,
        structured_stream=True,
        arguments_valid=True,
        arguments_match=True,
        finish_reason="tool_calls",
    )
    assert manager.select("code", requires_tools=True) == (
        "openrouter",
        "new-model:free",
    )


def test_strict_pool_requires_qualification_without_tools(tmp_path):
    manager = PoolManager(
        {
            "pools": {
                "privacy": PoolConfig(
                    name="privacy",
                    models=[
                        {"provider": "synthetic", "model": "qualified"},
                        {"provider": "synthetic", "model": "unknown"},
                    ],
                    zdr=True,
                    require_tool_qualification=True,
                ),
            },
            "quality_db_path": str(tmp_path / "quality.db"),
            "tool_capability_db_path": str(tmp_path / "capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"synthetic": "test-key"},
        }
    )
    manager._tool_capabilities.record(
        provider="synthetic",
        model="qualified",
        level=ToolCapabilityLevel.STRICT_STRUCTURED_STREAM,
        status="passed",
        http_status=200,
        tool_call_count=1,
        structured_stream=True,
        arguments_valid=True,
        arguments_match=True,
        finish_reason="tool_calls",
    )

    assert manager.select("privacy") == ("synthetic", "qualified")


def test_readiness_excludes_unqualified_models_from_strict_pool(tmp_path):
    manager = PoolManager(
        {
            "pools": {
                "privacy": PoolConfig(
                    name="privacy",
                    models=[
                        {"provider": "synthetic", "model": "qualified"},
                        {"provider": "synthetic", "model": "unknown"},
                    ],
                    zdr=True,
                    require_tool_qualification=True,
                ),
            },
            "quality_db_path": str(tmp_path / "quality.db"),
            "tool_capability_db_path": str(tmp_path / "capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"synthetic": "test-key"},
        }
    )
    manager._tool_capabilities.record(
        provider="synthetic",
        model="qualified",
        level=ToolCapabilityLevel.STRICT_STRUCTURED_STREAM,
        status="passed",
        http_status=200,
        tool_call_count=1,
        structured_stream=True,
        arguments_valid=True,
        arguments_match=True,
        finish_reason="tool_calls",
    )

    health, empty = manager.readiness_status()

    assert health["privacy"]["configured"] == 2
    assert health["privacy"]["selectable"] == 1
    assert empty == []


def test_failed_probe_keeps_auto_discovered_model_out_of_tool_pool(tmp_path):
    manager = _manager(str(tmp_path))
    manager.catalog_registry = _Registry()
    manager.extend_pools_with_auto_catalog()
    manager._tool_capabilities.record(
        provider="openrouter",
        model="new-model:free",
        level=ToolCapabilityLevel.STRUCTURED_STREAM,
        status="failed",
        failure_class="non_strict_tool_contract",
    )

    assert manager.select("code", requires_tools=True) is None


def test_recovery_probe_allows_curated_structured_model(tmp_path):
    """A curated model with a non-strict probe may be a bounded fallback."""
    manager = PoolManager(
        {
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "openrouter", "model": "new-model:free"}],
                ),
            },
            "quality_db_path": os.path.join(tmp_path, "quality.db"),
            "tool_capability_db_path": os.path.join(tmp_path, "capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openrouter": "test-key"},
        }
    )
    manager.catalog_registry = _Registry()
    manager._tool_capabilities.record(
        provider="openrouter",
        model="new-model:free",
        level=ToolCapabilityLevel.STRUCTURED_STREAM,
        status="failed",
        failure_class="non_strict_tool_contract",
    )

    assert manager.select("code", requires_tools=True) is None
    assert manager.select(
        "code",
        requires_tools=True,
        allow_unqualified_static_tools=True,
    ) == ("openrouter", "new-model:free")


def test_recovery_probe_allows_auto_discovered_structured_model(tmp_path):
    """A tested catalog model can be used when strict routes are exhausted."""
    manager = _manager(str(tmp_path))
    manager.catalog_registry = _Registry()
    manager.extend_pools_with_auto_catalog()
    manager._tool_capabilities.record(
        provider="openrouter",
        model="new-model:free",
        level=ToolCapabilityLevel.STRUCTURED_STREAM,
        status="failed",
        failure_class="non_strict_tool_contract",
    )

    assert manager.select("code", requires_tools=True) is None
    assert manager.select(
        "code",
        requires_tools=True,
        allow_cooldown_probe=True,
        allow_structured_tool_fallback=True,
    ) == ("openrouter", "new-model:free")


def test_compatibility_recovery_allows_curated_unsupported_model(tmp_path):
    """The last recovery pass can try a curated model with stale metadata."""
    manager = PoolManager(
        {
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "openrouter", "model": "curated-model"}],
                ),
            },
            "quality_db_path": os.path.join(tmp_path, "quality.db"),
            "tool_capability_db_path": os.path.join(tmp_path, "capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openrouter": "test-key"},
        }
    )
    manager._tool_capabilities.record(
        provider="openrouter",
        model="curated-model",
        level=ToolCapabilityLevel.UNSUPPORTED,
        status="failed",
        failure_class="unsupported",
    )

    assert manager.select("code", requires_tools=True) is None
    assert manager.select(
        "code",
        requires_tools=True,
        allow_cooldown_probe=True,
        allow_unqualified_static_tools=True,
        allow_structured_tool_fallback=True,
        allow_tool_compatibility_fallback=True,
    ) == ("openrouter", "curated-model")


def test_unavailable_probe_does_not_permanently_block_curated_model(tmp_path):
    """Provider quota/transport failures must recover after cooldown."""
    manager = PoolManager(
        {
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "openrouter", "model": "curated-model"}],
                ),
            },
            "quality_db_path": os.path.join(tmp_path, "quality.db"),
            "tool_capability_db_path": os.path.join(tmp_path, "capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openrouter": "test-key"},
        }
    )
    manager._tool_capabilities.record(
        provider="openrouter",
        model="curated-model",
        level=ToolCapabilityLevel.UNAVAILABLE,
        status="unavailable",
        http_status=429,
        failure_class="rate_limited",
    )

    assert manager.select("code", requires_tools=True) == (
        "openrouter",
        "curated-model",
    )


def test_unavailable_probe_keeps_auto_discovered_model_held_back(tmp_path):
    """An auto-discovered model still needs a successful qualification."""
    manager = _manager(str(tmp_path))
    manager.catalog_registry = _Registry()
    manager.extend_pools_with_auto_catalog()
    manager._tool_capabilities.record(
        provider="openrouter",
        model="new-model:free",
        level=ToolCapabilityLevel.UNAVAILABLE,
        status="unavailable",
        http_status=502,
        failure_class="upstream_error",
    )

    assert manager.select("code", requires_tools=True) is None


def test_unavailable_probe_is_retried_before_normal_probe_ttl(monkeypatch, tmp_path):
    db = ToolCapabilityDB(str(tmp_path / "capability.db"))
    monkeypatch.setattr("tusker_gateway.tool_qualification.time.time", lambda: 1_000.0)
    record = db.record(
        provider="openrouter",
        model="temporarily-down",
        level=ToolCapabilityLevel.UNAVAILABLE,
        status="unavailable",
        failure_class="rate_limited",
        checked_at=0.0,
    )

    assert _needs_probe(record, force=False, max_age_secs=86_400.0) is True


def test_qualification_skips_persisted_route_quarantine(tmp_path):
    from tusker_gateway.persistent_cooldown import PersistentCooldownStore

    store = PersistentCooldownStore(tmp_path / "cooldowns.db")
    store.record_provider("cohere", 60.0)

    assert _route_is_quarantined("cohere", "command", store) is True
    assert _route_is_quarantined("groq", "gpt-oss", store) is False


def test_strict_probe_result_requires_exact_tool_contract():
    passed = _result_from_stream(
        provider="openrouter",
        model="good",
        status_code=200,
        latency_ms=120.0,
        calls={0: {"name": PROBE_TOOL_NAME, "arguments": '{"path":"' + PROBE_PATH + '"}'}},
        text_parts=[],
        finish_reason="tool_calls",
    )
    assert passed["level"] == ToolCapabilityLevel.STRICT_STRUCTURED_STREAM
    assert passed["status"] == "passed"

    prose = _result_from_stream(
        provider="openrouter",
        model="prose",
        status_code=200,
        latency_ms=120.0,
        calls={0: {"name": PROBE_TOOL_NAME, "arguments": '{"path":"' + PROBE_PATH + '"}'}},
        text_parts=["I will call the tool."],
        finish_reason="tool_calls",
    )
    assert prose["level"] == ToolCapabilityLevel.STRUCTURED_STREAM
    assert prose["status"] == "passed"


def test_valid_structured_probe_with_prose_is_qualified(tmp_path):
    result = _result_from_stream(
        provider="openrouter",
        model="prose-but-structured",
        status_code=200,
        latency_ms=120.0,
        calls={0: {"name": PROBE_TOOL_NAME, "arguments": '{"path":"' + PROBE_PATH + '"}'}},
        text_parts=["I will call the tool."],
        finish_reason="tool_calls",
    )
    db = ToolCapabilityDB(str(tmp_path / "capability.db"))
    record = db.record(**result)

    assert record.level == ToolCapabilityLevel.STRUCTURED_STREAM
    assert record.qualified_for_tools is True


def test_structured_probe_with_wrong_tool_is_not_qualified(tmp_path):
    result = _result_from_stream(
        provider="openrouter",
        model="wrong-tool",
        status_code=200,
        latency_ms=120.0,
        calls={0: {"name": "other_tool", "arguments": '{"path":"' + PROBE_PATH + '"}'}},
        text_parts=[],
        finish_reason="tool_calls",
    )
    db = ToolCapabilityDB(str(tmp_path / "capability.db"))
    record = db.record(**result)

    assert record.level == ToolCapabilityLevel.STRUCTURED_STREAM
    assert record.arguments_match is False
    assert record.qualified_for_tools is False


def test_probe_transport_classification_is_bounded():
    assert _classify_http_failure(400, "tools are unsupported")[0] == ToolCapabilityLevel.UNSUPPORTED
    assert _classify_http_failure(429, "temporarily unavailable")[2] == "rate_limited"
    assert _is_account_quota_exhausted(
        "Quota exceeded for metric free_tier_requests, limit: 0"
    ) is True
    assert _classify_http_failure(
        429, "Quota exceeded for metric free_tier_requests, limit: 0"
    )[2] == "provider_quota"
    assert _classify_http_failure(
        502,
        "Rate limited",
        {"X-Tusker-Provider-Failure": "provider_quota"},
    )[2] == "provider_quota"
    assert _is_account_quota_exhausted(
        "Quota exceeded for model requests, limit: 0"
    ) is False
    assert _classify_http_failure(402, "Payment required")[2] == "quota"
    assert _classify_http_failure(502, "provider down")[0] == ToolCapabilityLevel.UNAVAILABLE


def test_catalog_registry_prefers_injected_live_rotators():
    """When the gateway's live rotators are supplied, the codex catalog
    client must use them as its token source instead of building a new
    read-only rotator (which fails with HTTP 401 on stale tokens)."""
    from tusker_gateway import tool_qualification as tq

    class _FakeRotator:
        async def get_token(self):
            return "live-token"

    config = {
        "providers": {"openai-codex": {}},
        "provider_api_keys": {},
        "credential_pools": {
            "openai-codex": [{"access_token": "a", "refresh_token": "r"}],
        },
    }
    registry = tq._catalog_registry(
        config,
        credential_rotators={"openai-codex": _FakeRotator()},
    )
    client = registry.get_client("openai-codex")
    assert client is not None
    assert client._token_source is not None
    # The token source must be the live rotator's bound method, not a
    # freshly constructed read-only rotator's.
    assert getattr(client._token_source, "__self__", None).__class__ is _FakeRotator


def test_catalog_registry_builds_read_only_rotator_without_live_rotators(monkeypatch):
    """Standalone runs (no injected rotators) keep the read-only rotator
    behaviour — refresh stays disabled so shared refresh tokens survive."""
    from tusker_gateway import tool_qualification as tq

    constructed: list[dict] = []

    class _RecordingRotator:
        def __init__(self, credentials, **kwargs):
            constructed.append(kwargs)
            self._kwargs = kwargs

        async def get_token(self):
            return None

    monkeypatch.setattr(
        "tusker_gateway.passthrough.CodexTokenRotator",
        _RecordingRotator,
    )
    config = {
        "providers": {"openai-codex": {}},
        "provider_api_keys": {},
        "credential_pools": {
            "openai-codex": [{"access_token": "a", "refresh_token": "r"}],
        },
    }
    registry = tq._catalog_registry(config)
    client = registry.get_client("openai-codex")
    assert client is not None
    assert len(constructed) == 1
    assert constructed[0].get("refresh_enabled") is False
