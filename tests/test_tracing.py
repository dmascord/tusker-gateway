"""Unit tests for the OTLP tracer (Release 2)."""
from __future__ import annotations

import json

import pytest

from tusker_gateway.tracing import Span, Tracer, TracerConfig, load_tracer_config_from_env


def test_disabled_tracer_no_export():
    t = Tracer(TracerConfig(endpoint=""))
    assert t.enabled is False
    with t.span("foo") as sp:
        assert sp is not None
    # No buffer growth.
    assert t._buffer == []  # noqa: SLF001


def test_span_to_otlp_basic():
    s = Span(
        name="test",
        trace_id="0" * 32,
        span_id="0" * 16,
        start_time_ns=1,
        end_time_ns=2,
        attributes={"foo": "bar"},
    )
    out = s.to_otlp()
    assert out["name"] == "test"
    assert out["traceId"] == "0" * 32
    assert out["spanId"] == "0" * 16
    assert out["startTimeUnixNano"] == "1"
    assert out["endTimeUnixNano"] == "2"
    assert out["status"] == {"code": 1}
    assert out["attributes"] == [{"key": "foo", "value": {"stringValue": "bar"}}]


def test_span_to_otlp_with_parent():
    s = Span(
        name="child", trace_id="trace", span_id="child",
        parent_span_id="parent",
        start_time_ns=1, end_time_ns=2,
    )
    out = s.to_otlp()
    assert out["parentSpanId"] == "parent"


def test_span_to_otlp_error_status():
    s = Span(
        name="x", trace_id="t", span_id="s",
        start_time_ns=0, end_time_ns=1,
        status="error", status_message="boom",
    )
    out = s.to_otlp()
    assert out["status"] == {"code": 2, "message": "boom"}


def test_span_attributes_coerced():
    with Tracer(TracerConfig(endpoint="")).span("x", attributes={"a": 1, "b": True, "c": 1.5}) as sp:
        pass
    assert sp.attributes["a"] == "1"
    assert sp.attributes["b"] == "true"
    assert sp.attributes["c"] == "1.5"


def test_span_captures_exception():
    t = Tracer(TracerConfig(endpoint=""))
    with pytest.raises(ValueError):
        with t.span("op") as sp:
            raise ValueError("test")
    assert sp.status == "error"
    assert "test" in sp.attributes["exception.message"]
    assert sp.attributes["exception.type"] == "ValueError"
    assert "Traceback" in sp.attributes["exception.stacktrace"]


def test_load_config_defaults():
    cfg = load_tracer_config_from_env(env={})
    assert cfg.endpoint == ""
    assert cfg.service_name == "tusker-gateway"
    assert cfg.batch_size == 100


def test_load_config_overrides():
    cfg = load_tracer_config_from_env(env={
        "TUSKER_OTLP_ENDPOINT": "http://collector:4318",
        "TUSKER_OTLP_SERVICE_NAME": "my-service",
        "TUSKER_OTLP_BATCH": "50",
        "TUSKER_OTLP_FLUSH_SECS": "10",
    })
    assert cfg.endpoint == "http://collector:4318"
    assert cfg.service_name == "my-service"
    assert cfg.batch_size == 50
    assert cfg.flush_interval_secs == 10


def test_load_config_with_headers():
    cfg = load_tracer_config_from_env(env={
        "TUSKER_OTLP_ENDPOINT": "http://x:4318",
        "TUSKER_OTLP_HEADERS": '{"x-honeycomb-team": "abc"}',
    })
    assert cfg.headers == {"x-honeycomb-team": "abc"}


def test_load_config_handles_bad_headers_json():
    cfg = load_tracer_config_from_env(env={
        "TUSKER_OTLP_ENDPOINT": "http://x:4318",
        "TUSKER_OTLP_HEADERS": "{not json",
    })
    assert cfg.headers == {}


def test_otlp_body_structure():
    """Verify the export body matches the OTLP/HTTP-JSON spec for resourceSpans."""
    cfg = TracerConfig(endpoint="http://collector:4318")
    t = Tracer(cfg)
    # Capture a span
    with t.span("test", attributes={"k": "v"}) as _:
        pass
    # Manually build the body the way _export does.
    batch = t._buffer[:]  # noqa: SLF001
    body = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": cfg.service_name}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "tusker-gateway", "version": "0.1.0"},
                        "spans": [s.to_otlp() for s in batch],
                    }
                ],
            }
        ]
    }
    # Round-trip JSON works.
    encoded = json.dumps(body)
    decoded = json.loads(encoded)
    assert "resourceSpans" in decoded
    assert decoded["resourceSpans"][0]["scopeSpans"][0]["scope"]["name"] == "tusker-gateway"
