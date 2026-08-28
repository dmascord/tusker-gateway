"""Tests for behavioral tool-call qualification and pool gating."""
from __future__ import annotations

import os

from tusker_gateway.config import PoolConfig
from tusker_gateway.pools import PoolManager
from tusker_gateway.tool_capability import (
    TOOL_CAPABILITY_PROBE_VERSION,
    ToolCapabilityDB,
    ToolCapabilityLevel,
)
from tusker_gateway.tool_qualification import (
    PROBE_PATH,
    PROBE_TOOL_NAME,
    _classify_http_failure,
    _result_from_stream,
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
                    auto_free=True,
                ),
            },
            "quality_db_path": os.path.join(tmp_path, "quality.db"),
            "tool_capability_db_path": os.path.join(tmp_path, "capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"openrouter": "test-key"},
        }
    )


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


def test_auto_discovered_model_requires_strict_stream_probe(tmp_path):
    manager = _manager(str(tmp_path))
    manager.catalog_registry = _Registry()
    manager.extend_pools_with_free_catalog()

    assert manager.auto_added["code"] == {("openrouter", "new-model:free")}
    assert manager.select("code", requires_tools=True) is None

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


def test_failed_probe_keeps_auto_discovered_model_out_of_tool_pool(tmp_path):
    manager = _manager(str(tmp_path))
    manager.catalog_registry = _Registry()
    manager.extend_pools_with_free_catalog()
    manager._tool_capabilities.record(
        provider="openrouter",
        model="new-model:free",
        level=ToolCapabilityLevel.STRUCTURED_STREAM,
        status="failed",
        failure_class="non_strict_tool_contract",
    )

    assert manager.select("code", requires_tools=True) is None


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
    assert prose["status"] == "failed"


def test_probe_transport_classification_is_bounded():
    assert _classify_http_failure(400, "tools are unsupported")[0] == ToolCapabilityLevel.UNSUPPORTED
    assert _classify_http_failure(429, "temporarily unavailable")[2] == "rate_limited"
    assert _classify_http_failure(502, "provider down")[0] == ToolCapabilityLevel.UNAVAILABLE
