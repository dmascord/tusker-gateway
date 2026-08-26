"""Tests for RTK metric instrumentation.

We verify that ``compress_text`` and ``compress_tool_results`` populate
the RTK counters on ``MetricsRegistry`` for every observable outcome
(compressed, no_match, no_savings, skipped_short, skipped_too_large,
empty, non_string_content) and that the bytes-saved counter is
accurate.

The metrics are best-effort — passing ``metrics=None`` must still work
(no crash, no implicit dependency).
"""
from __future__ import annotations

import pytest

from tusker_gateway.metrics import MetricsRegistry
from tusker_gateway.rtk import (
    compress_text,
    compress_tool_results,
    set_enabled,
)


@pytest.fixture(autouse=True)
def _reset_rtk_state():
    """Each test starts with RTK disabled; tests opt in explicitly."""
    prev = set_enabled(False) if False else None
    was_enabled = compress_tool_results([]) is not None  # cheap no-op check
    set_enabled(False)
    yield
    set_enabled(False)


def _registry() -> MetricsRegistry:
    """Fresh MetricsRegistry per test — avoids cross-test bleed."""
    return MetricsRegistry()


# ---------------------------------------------------------------------------
# compress_text: no-metrics path
# ---------------------------------------------------------------------------


def test_compress_text_works_without_metrics():
    """compress_text must not crash or import metrics when none are passed."""
    out = compress_text(
        "diff --git a/x.py b/x.py\n@@ -1,1 +1,1 @@\n-old\n+new\n" * 5,
    )
    assert "diff --git" in out


# ---------------------------------------------------------------------------
# compress_text: outcome coverage
# ---------------------------------------------------------------------------


GIT_DIFF_LARGE = """diff --git a/src/x.py b/src/x.py
index abc..def 100644
--- a/src/x.py
+++ b/src/x.py
@@ -1,5 +1,7 @@
 context line 1
 context line 2
 context line 3
-removed line 1
-removed line 2
+added line 1
+added line 2
+added line 3
+added line 4
 context line 4
"""

PYTEST_LARGE = """============================= test session starts ==============================
platform linux -- Python 3.14.6, pytest-8.4.0
collected 50 items

tests/test_a.py::test_x ........... PASSED                                 [ 22%]
tests/test_a.py::test_y ........... PASSED                                 [ 44%]
tests/test_a.py::test_z ........... PASSED                                 [ 66%]
tests/test_a.py::test_w ........... FAILED                                 [ 88%]
tests/test_a.py::test_v ........... PASSED                                 [100%]

FAILED tests/test_a.py::test_w - AssertionError: x != y
=================== 1 failed, 49 passed in 0.42s =====================
"""


def test_compress_text_records_compressed_outcome():
    metrics = _registry()
    out = compress_text(GIT_DIFF_LARGE, metrics=metrics)
    assert out != GIT_DIFF_LARGE  # actually compressed
    assert metrics.rtk_calls.get({"outcome": "compressed"}) == 1
    # Filter label is set on the block counter.
    assert metrics.rtk_blocks.get(
        {"filter": "git-diff", "outcome": "compressed"}
    ) == 1
    # Bytes saved is the size difference.
    saved = len(GIT_DIFF_LARGE) - len(out)
    assert metrics.rtk_bytes_saved.get() == saved


def test_compress_text_records_no_match_for_prose():
    metrics = _registry()
    sample = (
        "This is some regular prose. " * 30
        + "No filters should match this. " * 30
    )
    assert len(sample) >= 200
    out = compress_text(sample, metrics=metrics)
    assert out == sample
    # Either no_match (nothing matched any filter) or no_savings (something
    # matched but didn't save enough). Both are valid "didn't compress".
    no_match = metrics.rtk_calls.get({"outcome": "no_match"})
    no_savings = metrics.rtk_calls.get({"outcome": "no_savings"})
    assert (no_match + no_savings) == 1
    assert metrics.rtk_calls.get({"outcome": "compressed"}) == 0
    assert metrics.rtk_bytes_saved.get() == 0


def test_compress_text_records_skipped_short():
    metrics = _registry()
    out = compress_text("too short to compress", metrics=metrics)
    assert out == "too short to compress"
    assert metrics.rtk_calls.get({"outcome": "skipped_short"}) == 1


def test_compress_text_records_skipped_too_large():
    metrics = _registry()
    big = "diff --git a/x.py b/x.py\n@@ ...\n" * 5000  # ~150 KB
    out = compress_text(big, metrics=metrics)
    assert out == big  # unchanged
    assert metrics.rtk_calls.get({"outcome": "skipped_too_large"}) == 1


def test_compress_text_records_empty():
    metrics = _registry()
    out = compress_text("", metrics=metrics)
    assert out == ""
    assert metrics.rtk_calls.get({"outcome": "empty"}) == 1


def test_compress_text_label_per_filter():
    """Different filters produce different (filter, outcome) labels."""
    cases = [
        (GIT_DIFF_LARGE, "git-diff"),
        (PYTEST_LARGE, "pytest"),
    ]
    for sample, expected_filter in cases:
        metrics = _registry()
        compress_text(sample, metrics=metrics)
        assert metrics.rtk_blocks.get(
            {"filter": expected_filter, "outcome": "compressed"}
        ) == 1, f"{expected_filter} didn't fire"


# ---------------------------------------------------------------------------
# compress_tool_results: aggregation
# ---------------------------------------------------------------------------


def test_compress_tool_results_aggregates_across_messages():
    set_enabled(True)
    metrics = _registry()
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "what happened?"},
        {"role": "tool", "tool_call_id": "1", "content": GIT_DIFF_LARGE},
        {"role": "tool", "tool_call_id": "2", "content": PYTEST_LARGE},
        {"role": "tool", "tool_call_id": "3", "content": "short"},
    ]
    out = compress_tool_results(messages, metrics=metrics)
    # System/user are passed through unchanged.
    assert out[0] is messages[0]
    assert out[1] is messages[1]
    # tool[1] and tool[2] are compressed (different content).
    assert out[2]["content"] != GIT_DIFF_LARGE
    assert out[3]["content"] != PYTEST_LARGE
    # tool[3] is too short, so it's left alone (same object).
    assert out[4] is messages[4]
    # Two compressed blocks + one skipped_short.
    assert metrics.rtk_calls.get({"outcome": "compressed"}) == 2
    assert metrics.rtk_calls.get({"outcome": "skipped_short"}) == 1


def test_compress_tool_results_skips_non_string_content():
    set_enabled(True)
    metrics = _registry()
    messages = [{
        "role": "tool",
        "tool_call_id": "1",
        "content": [{"type": "text", "text": "structured"}],  # not a string
    }]
    out = compress_tool_results(messages, metrics=metrics)
    # Untouched because non-string content.
    assert out[0] is messages[0]
    assert metrics.rtk_calls.get({"outcome": "non_string_content"}) == 1


def test_compress_tool_results_bytes_saved_aggregates():
    set_enabled(True)
    metrics = _registry()
    messages = [
        {"role": "tool", "tool_call_id": "1", "content": GIT_DIFF_LARGE},
        {"role": "tool", "tool_call_id": "2", "content": PYTEST_LARGE},
    ]
    compress_tool_results(messages, metrics=metrics)
    expected = (len(GIT_DIFF_LARGE) - len(compress_text(GIT_DIFF_LARGE))) + (
        len(PYTEST_LARGE) - len(compress_text(PYTEST_LARGE))
    )
    assert metrics.rtk_bytes_saved.get() == expected


def test_compress_tool_results_no_metrics_still_works():
    """Backward compatibility: compress_tool_results with no metrics= works."""
    set_enabled(True)
    messages = [{"role": "tool", "tool_call_id": "1", "content": GIT_DIFF_LARGE}]
    out = compress_tool_results(messages)
    assert out[0]["content"] != GIT_DIFF_LARGE


# ---------------------------------------------------------------------------
# Metrics surface: /metrics endpoint
# ---------------------------------------------------------------------------


def test_metrics_registry_renders_rtk_lines():
    """The new RTK metrics appear in the /metrics output."""
    metrics = _registry()
    metrics.rtk_blocks.inc({"filter": "git-diff", "outcome": "compressed"})
    metrics.rtk_blocks.inc({"filter": "pytest", "outcome": "compressed"})
    metrics.rtk_bytes_saved.inc(amount=1234)
    metrics.rtk_calls.inc({"outcome": "compressed"})
    rendered = metrics.render()
    assert "tusker_rtk_blocks_total" in rendered
    assert 'filter="git-diff"' in rendered
    assert 'filter="pytest"' in rendered
    assert "tusker_rtk_bytes_saved_total 1234" in rendered
    assert "tusker_rtk_calls_total" in rendered
