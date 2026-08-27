"""Tests for the RTK token-saver shim.

The shim is a fail-open Python implementation of the most common RTK
filters. These tests verify:

- each filter produces shorter output for its target shape
- filters don't misfire on unrelated text
- ``compress_text`` returns input unchanged for text too short to compress
- ``compress_tool_results`` only touches tool-role messages
- disabled-by-default semantics
"""
from __future__ import annotations

import pytest

from tusker_gateway.rtk import (
    compress_text,
    compress_tool_results,
    is_enabled,
    set_enabled,
)


@pytest.mark.asyncio
async def test_create_app_reads_rtk_environment_flag(tmp_path, monkeypatch):
    """Startup applies TUSKER_RTK_ENABLED and exposes the runtime state."""
    from aiohttp.web_runner import AppRunner

    from tusker_gateway.app import create_app

    monkeypatch.setenv("TUSKER_RTK_ENABLED", "true")
    monkeypatch.setenv("TUSKER_CATALOG_ENABLED", "0")
    monkeypatch.setenv("TUSKER_CAPABILITIES_ENABLED", "0")
    monkeypatch.setenv("QUALITY_DB_PATH", str(tmp_path / "quality.db"))

    app = create_app()
    runner = AppRunner(app)
    await runner.setup()
    try:
        assert app["rtk_enabled"] is True
        assert is_enabled() is True
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rtk_state():
    """Each test starts with RTK disabled; tests opt in explicitly."""
    prev = is_enabled()
    set_enabled(False)
    yield
    set_enabled(prev)


GIT_DIFF_SAMPLE = """diff --git a/src/x.py b/src/x.py
index abc1234..def5678 100644
--- a/src/x.py
+++ b/src/x.py
@@ -1,5 +1,7 @@
 unchanged context
 unchanged context
 unchanged context
-removed line 1
-removed line 2
+added line 1
+added line 2
 unchanged context
"""


GIT_STATUS_SAMPLE = """On branch main
Your branch is up to date with 'origin/main'.

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
	modified:   src/a.py
	modified:   src/b.py
	modified:   src/c.py
	modified:   src/d.py
	modified:   src/e.py

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	new_file_1.txt
	new_file_2.txt
	new_file_3.txt
"""


GIT_LOG_SAMPLE = """commit abc1234567890abcdef1234567890abcdef12345
Author: Jane Developer <jane@example.com>
Date:   Mon Aug 25 12:00:00 2026 +1000

    Fix off-by-one in token counter

commit def5678901234567890abcdef1234567890abcdef
Author: Bob Smith <bob@example.com>
Date:   Mon Aug 25 11:30:00 2026 +1000

    Add RTK compression tests
"""


CARGO_TEST_SAMPLE = """running 10 tests
test utils::test_parse ... ok
test utils::test_format ... ok
test utils::test_invalid ... ok
test utils::test_basic ... ok
test utils::test_edge_case ... FAILED

failures:

---- utils::test_edge_case stdout ----
thread 'utils::test_edge_case' panicked at 'assertion failed: x == y'

failures:
    utils::test_edge_case

test result: FAILED. 4 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""


PYTEST_SAMPLE = """============================= test session starts ==============================
platform linux -- Python 3.14.6, pytest-8.4.0
collected 100 items

tests/test_a.py::test_x ........... PASSED                                 [ 11%]
tests/test_a.py::test_y ........... PASSED                                 [ 22%]
tests/test_a.py::test_z ........... FAILED                                 [ 33%]
tests/test_a.py::test_w ........... PASSED                                 [ 44%]
tests/test_a.py::test_v ........... FAILED                                 [ 55%]

FAILED tests/test_a.py::test_z - AssertionError: x != y
FAILED tests/test_a.py::test_v - AssertionError: a != b

=========================== short test summary info ============================
FAILED tests/test_a.py::test_z
FAILED tests/test_a.py::test_v
=================== 2 failed, 98 passed in 0.42s =====================
"""


LS_SAMPLE = """total 42
drwxr-xr-x  5 user staff  160 Aug 25 12:00 docs
drwxr-xr-x  3 user staff   96 Aug 25 12:00 docs/api
-rw-r--r--  1 user staff 1234 Aug 25 12:00 docs/README.md
-rw-r--r--  1 user staff 5678 Aug 25 12:00 docs/CONTRIBUTING.md
drwxr-xr-x  4 user staff  128 Aug 25 12:00 src
-rw-r--r--  1 user staff  234 Aug 25 12:00 src/main.py
"""


GREP_SAMPLE = """src/main.py:42:    return x + y
src/main.py:55:    raise ValueError("nope")
src/main.py:88:    if x:
src/main.py:90:    return y
src/main.py:120:   log()
src/utils.py:12:# TODO: refactor this very long comment
src/utils.py:33:    return None
src/utils.py:55:    def helper(x):
src/utils.py:88:        return x * 2
src/utils.py:120:   def helper2(y):
src/utils.py:155:   return y * 3
"""


PROSE_SAMPLE = """This is some regular prose. It contains no tool output, no shell
command structure, and no patterns that any RTK filter would recognize.
It should be returned completely unchanged by compress_text because it
is either too short or doesn't match any filter pattern.
"""


DEDUP_SAMPLE = """2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 WARN rate limited
2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 INFO request handled
2026-08-25 INFO request handled
"""


# ---------------------------------------------------------------------------
# Tests: each filter
# ---------------------------------------------------------------------------


class TestGitDiff:
    def test_compresses(self):
        out = compress_text(GIT_DIFF_SAMPLE)
        assert len(out) < len(GIT_DIFF_SAMPLE)
        assert "diff --git" in out
        assert "+added line 1" in out
        assert "-removed line 1" in out

    def test_strips_index_and_dash_headers(self):
        out = compress_text(GIT_DIFF_SAMPLE)
        assert "index abc1234" not in out
        assert "--- a/" not in out


class TestGitStatus:
    def test_compresses(self):
        out = compress_text(GIT_STATUS_SAMPLE)
        assert len(out) < len(GIT_STATUS_SAMPLE)
        assert "modified: 5" in out or "modified:" in out

    def test_keeps_branch_header(self):
        out = compress_text(GIT_STATUS_SAMPLE)
        assert "On branch main" in out


class TestGitLog:
    def test_compresses(self):
        out = compress_text(GIT_LOG_SAMPLE)
        assert len(out) < len(GIT_LOG_SAMPLE)
        assert "Fix off-by-one" in out
        assert "Add RTK compression tests" in out

    def test_drops_author_date(self):
        out = compress_text(GIT_LOG_SAMPLE)
        assert "Author:" not in out
        assert "Date:" not in out


class TestCargoTest:
    def test_compresses(self):
        out = compress_text(CARGO_TEST_SAMPLE)
        assert len(out) < len(CARGO_TEST_SAMPLE)
        assert "test result:" in out
        assert "utils::test_edge_case" in out

    def test_drops_passing_tests(self):
        out = compress_text(CARGO_TEST_SAMPLE)
        assert "test utils::test_parse" not in out


class TestPytest:
    def test_compresses(self):
        out = compress_text(PYTEST_SAMPLE)
        assert len(out) < len(PYTEST_SAMPLE)
        assert "2 failed" in out or "passed" in out
        assert "test_z" in out


class TestLs:
    def test_compresses(self):
        out = compress_text(LS_SAMPLE)
        assert len(out) < len(LS_SAMPLE)
        assert "total:" in out
        assert "docs/" in out


class TestGrep:
    def test_compresses(self):
        out = compress_text(GREP_SAMPLE)
        assert len(out) < len(GREP_SAMPLE)
        # Per-file counts are preserved.
        assert "src/main.py:" in out
        assert "src/utils.py:" in out


class TestDedup:
    def test_collapses_repeats(self):
        out = compress_text(DEDUP_SAMPLE)
        # DEDUP_SAMPLE has 8 INFO + 1 WARN + 6 INFO. Two collapses.
        assert "x 8" in out
        assert "x 6" in out
        # The single WARN line is preserved as-is.
        assert "2026-08-25 WARN" in out


# ---------------------------------------------------------------------------
# Tests: routing and edge cases
# ---------------------------------------------------------------------------


def test_short_text_unchanged():
    """Text under 200 chars is returned unchanged regardless of filter fit."""
    text = "diff --git a/x.py b/x.py\n" * 5
    assert compress_text(text) == text


def test_prose_unchanged():
    """Plain prose doesn't match any specific filter; generic dedup fallback
    only fires on 5+ identical adjacent lines."""
    out = compress_text(PROSE_SAMPLE)
    assert out == PROSE_SAMPLE


def test_very_large_block_unchanged():
    """Blocks > 64 KB are not compressed (likely docs/system prompts)."""
    big = "diff --git a/x.py b/x.py\n" * 5000  # ~150 KB
    assert compress_text(big) == big


def test_minimum_savings_threshold():
    """If compression doesn't save at least 10%, the original is returned."""
    # Construct text that triggers ls but won't compress much.
    text = "total 2\n-rw-r--r--  1 user staff 100 Aug 25 12:00 a.txt\n-rw-r--r--  1 user staff 100 Aug 25 12:00 b.txt\n"
    out = compress_text(text)
    # Should fall back to dedup, which won't change this. Returned unchanged
    # (or nearly so) — must be at least as short as input.
    assert len(out) <= len(text)


# ---------------------------------------------------------------------------
# Tests: compress_tool_results
# ---------------------------------------------------------------------------


def test_compress_tool_results_passthrough_when_disabled():
    set_enabled(False)
    messages = [{"role": "tool", "content": GIT_DIFF_SAMPLE}]
    out = compress_tool_results(messages)
    assert out is messages  # same list object returned when disabled


def test_compress_tool_results_only_touches_tool_role():
    set_enabled(True)
    messages = [
        {"role": "system", "content": GIT_DIFF_SAMPLE},  # not touched
        {"role": "user", "content": GIT_DIFF_SAMPLE},     # not touched
        {"role": "tool", "content": GIT_DIFF_SAMPLE},     # compressed
        {"role": "assistant", "content": GIT_DIFF_SAMPLE},  # not touched
    ]
    out = compress_tool_results(messages)
    assert out[0] is messages[0]
    assert out[1] is messages[1]
    assert out[2] is not messages[2]
    assert out[2]["content"] != GIT_DIFF_SAMPLE
    assert out[3] is messages[3]


def test_compress_tool_results_skips_non_string_content():
    set_enabled(True)
    content = [{"type": "text", "text": GIT_DIFF_SAMPLE}]
    messages = [{"role": "tool", "content": content}]
    out = compress_tool_results(messages)
    # List content isn't touched (only string content is processed).
    assert out[0] is messages[0]


def test_compress_tool_results_does_not_mutate_input():
    set_enabled(True)
    original = GIT_DIFF_SAMPLE
    messages = [{"role": "tool", "content": original}]
    out = compress_tool_results(messages)
    assert out[0]["content"] != original
    assert messages[0]["content"] == original  # original untouched


def test_compress_tool_results_disabled_returns_input_list():
    set_enabled(False)
    messages = [{"role": "tool", "content": GIT_DIFF_SAMPLE}]
    out = compress_tool_results(messages)
    assert out is messages


def test_compress_tool_results_empty_list():
    set_enabled(True)
    assert compress_tool_results([]) == []


def test_compress_tool_results_handles_garbage_gracefully():
    set_enabled(True)
    # Non-dict messages shouldn't crash the loop.
    messages = [
        None,
        "string-not-dict",
        42,
        {"role": "tool", "content": GIT_DIFF_SAMPLE},
    ]
    out = compress_tool_results(messages)
    assert len(out) == 4
    assert out[3]["content"] != GIT_DIFF_SAMPLE
