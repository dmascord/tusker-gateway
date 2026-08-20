"""Unit tests for the Prometheus metrics exporter (Release 1)."""
from __future__ import annotations

import pytest

from tusker_gateway.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    DEFAULT_LATENCY_BUCKETS,
)


def test_counter_inc_unlabelled():
    c = Counter("c_total", "help")
    c.inc()
    c.inc(amount=4)
    assert c.get() == 5
    lines = list(c.render())
    assert any("c_total 5" in l for l in lines)


def test_counter_inc_labelled():
    c = Counter("c_total", "help", label_names=("a", "b"))
    c.inc({"a": "x", "b": "y"})
    c.inc({"a": "x", "b": "y"}, amount=2)
    c.inc({"a": "x", "b": "z"})
    lines = "\n".join(c.render())
    assert 'c_total{a="x",b="y"} 3' in lines
    assert 'c_total{a="x",b="z"} 1' in lines


def test_counter_render_escapes_quotes():
    c = Counter("c_total", "help", label_names=("k",))
    c.inc({"k": 'value"with"quotes'})
    lines = "\n".join(c.render())
    assert 'k="value\\"with\\"quotes"' in lines


def test_gauge_set_and_overwrite():
    g = Gauge("g", "help")
    g.set(None, 10)
    g.set(None, 5)
    assert g.get() == 5


def test_gauge_inc_dec():
    g = Gauge("g", "help", label_names=("k",))
    g.inc({"k": "a"})
    g.inc({"k": "a"}, amount=4)
    g.dec({"k": "a"}, amount=2)
    assert g.get({"k": "a"}) == 3


def test_histogram_observe_buckets():
    h = Histogram("h", "help", buckets=(1.0, 2.0, 5.0))
    h.observe(0.5)
    h.observe(1.5)
    h.observe(3.0)
    out = "\n".join(h.render())
    assert 'h_bucket{le="1"} 1' in out
    assert 'h_bucket{le="2"} 2' in out
    assert 'h_bucket{le="5"} 3' in out
    assert 'h_bucket{le="+Inf"} 3' in out
    assert 'h_sum{} 5' in out  # 0.5 + 1.5 + 3.0
    assert 'h_count{} 3' in out


def test_histogram_with_labels():
    h = Histogram("h", "help", label_names=("k",), buckets=(1.0,))
    h.observe(0.5, {"k": "a"})
    h.observe(2.0, {"k": "a"})
    h.observe(0.5, {"k": "b"})
    out = "\n".join(h.render())
    # Both label combinations represented.
    assert 'h_bucket{k="a",le="1"} 1' in out
    assert 'h_bucket{k="b",le="1"} 1' in out
    assert 'h_count{k="a"} 2' in out
    assert 'h_count{k="b"} 1' in out


def test_registry_render_includes_all_metrics():
    m = MetricsRegistry()
    m.requests_total.inc({"pool": "code", "provider": "p", "model": "m", "status": "ok"})
    m.tokens_total.inc({"pool": "code", "provider": "p", "model": "m", "direction": "prompt"}, 100)
    m.request_duration.observe(0.5, {"pool": "code", "provider": "p", "model": "m"})
    m.cache_hits.inc()
    m.cache_misses.inc()
    m.budget_blocks.inc({"kind": "daily"})
    m.set_pool_candidates("code", 32, 0)
    m.set_cooldowns_active([("p", "m"), ("q", "n")])

    out = m.render()
    assert "tusker_requests_total" in out
    assert "tusker_tokens_total" in out
    assert "tusker_request_duration_seconds_bucket" in out
    assert "tusker_cache_hits_total" in out
    assert "tusker_cache_misses_total" in out
    assert "tusker_budget_blocks_total" in out
    assert "tusker_pool_candidates" in out
    assert "tusker_cooldowns_active" in out
    assert "# HELP" in out and "# TYPE" in out
    # +Inf bucket present
    assert 'le="+Inf"' in out


def test_format_float_handles_int_and_specials():
    from tusker_gateway.metrics import _format_float
    assert _format_float(1.0) == "1"
    assert _format_float(0.5) in ("0.5", "0.5000000000000001")
    # NaN/+Inf are valid Prometheus exposition values.
    assert _format_float(float("inf")) == "+Inf"
    assert _format_float(float("-inf")) == "-Inf"
    assert _format_float(float("nan")) == "NaN"


def test_escape_replaces_special_chars():
    from tusker_gateway.metrics import _escape
    assert _escape('a"b') == 'a\\"b'
    assert _escape("a\\b") == "a\\\\b"
    assert _escape("a\nb") == "a\\nb"
