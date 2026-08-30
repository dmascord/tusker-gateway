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
    _needs_probe,
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


def test_auto_discovered_model_requires_qualified_stream_probe(tmp_path):
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
    manager.extend_pools_with_free_catalog()
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
    manager.extend_pools_with_free_catalog()
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
    assert _classify_http_failure(502, "provider down")[0] == ToolCapabilityLevel.UNAVAILABLE
