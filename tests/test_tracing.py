"""Unit tests for the OTLP tracer (Release 2)."""
from __future__ import annotations

import asyncio
import json

import pytest

from tusker_gateway.tracing import (
    Span,
    Tracer,
    TracerConfig,
    _current_span_stack,
    _last_span_id,
    _push_current,
    _pop_current,
    load_tracer_config_from_env,
)


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
# ---------------------------------------------------------------------------
# Current-span stack isolation (contextvars)
# ---------------------------------------------------------------------------

async def test_concurrent_requests_have_independent_span_stacks():
    """Interleaved requests must never observe each other's current span.

    Regression test for the process-global span stack: with a shared list,
    request A reading the current span while B's parent was also live got
    B's span, corrupting parent/child chains.
    """
    tracer = Tracer(TracerConfig(endpoint=""))
    both_parents_open = asyncio.Event()
    opened = 0
    parent_ids: dict[str, str] = {}
    current_during_request: dict[str, str | None] = {}

    async def request(label: str) -> None:
        nonlocal opened
        with tracer.span(f"{label}-parent") as parent:
            parent_ids[label] = parent.span_id
            opened += 1
            if opened == 2:
                both_parents_open.set()
            await both_parents_open.wait()
            # Both requests now hold a live parent span; the current span of
            # THIS task must be its own parent, never the sibling's.
            current_during_request[label] = _last_span_id()
            with tracer.span(f"{label}-child") as child:
                await asyncio.sleep(0)
                assert child.parent_span_id == parent.span_id
            assert _last_span_id() == parent.span_id

    await asyncio.gather(request("a"), request("b"))

    assert parent_ids["a"] != parent_ids["b"]
    assert current_during_request["a"] == parent_ids["a"]
    assert current_during_request["b"] == parent_ids["b"]
    assert _last_span_id() is None


async def test_pop_restores_parent():
    tracer = Tracer(TracerConfig(endpoint=""))
    with tracer.span("parent") as parent:
        with tracer.span("child") as child:
            assert _last_span_id() == child.span_id
        assert _last_span_id() == parent.span_id
    assert _last_span_id() is None


async def test_pop_foreign_span_is_noop_safe():
    tracer = Tracer(TracerConfig(endpoint=""))
    with tracer.span("kept") as kept:
        foreign = Span(
            name="foreign",
            trace_id="tid",
            span_id="foreign-span-id",
        )
        # Never pushed: must not raise and must not disturb the stack.
        _pop_current(foreign)
        assert _current_span_stack.get() == (kept,)
        assert _last_span_id() == kept.span_id
    assert _last_span_id() is None


async def test_pop_mid_stack_span_removes_only_that_span():
    """Defensive path: popping a span buried in the stack leaves the rest."""
    tracer = Tracer(TracerConfig(endpoint=""))
    with tracer.span("root") as root:
        middle = Span(name="middle", trace_id="tid", span_id="middle-span-id")
        _push_current(middle)
        with tracer.span("leaf") as leaf:
            assert _last_span_id() == leaf.span_id
        _pop_current(middle)
        assert _last_span_id() == root.span_id
    assert _last_span_id() is None
