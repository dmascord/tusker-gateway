"""RTK (RTK) token-saver shim.

Borrowed from 9router's ``open-sse/rtk/`` pre-translate hook. The upstream
project is a Rust binary that rewrites shell command output before the LLM
sees it (rtk-ai/rtk); we don't need the binary here — we just need the
filter logic that operates on already-captured ``tool_result`` content
in a chat-completion request.

This shim is deliberately small and pure-Python. It detects the most
common tool-output shapes (git diff/status/log, ls, find, grep, cargo
test, pytest) by **content fingerprint** and applies one filter per piece
of content, returning the smaller of the original or the compressed
result. Falls back to a generic dedup filter if no specific filter matches.

**Fails open**: any exception returns the original text untouched.

Disabled by default. Enable via ``TUSKER_RTK_ENABLED=true``. The
``X-Rtk-Token-Saver: off`` request header lets clients opt out
per-request (matches 9router's ``X-9Router-Token-Saver`` pattern).

Usage::

    from tusker_gateway.rtk import compress_tool_results
    openai_body = compress_tool_results(openai_body)
"""
from __future__ import annotations

import logging
import re
from collections import Counter, OrderedDict
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------

_ENABLED: bool = False


def is_enabled() -> bool:
    """Return whether RTK compression is currently enabled."""
    return _ENABLED


def set_enabled(flag: bool) -> None:
    """Enable or disable RTK compression at runtime (used by config and tests)."""
    global _ENABLED
    _ENABLED = bool(flag)


# ---------------------------------------------------------------------------
# Filter primitives
# ---------------------------------------------------------------------------

# A filter: (matches_fn, apply_fn). Both are pure functions of `text`.
# ``apply_fn`` MUST never raise — it should be best-effort and bounded.
FilterFn = Callable[[str], str]
MatchFn = Callable[[str], bool]


def _make_filter(
    name: str, matches: MatchFn, apply: FilterFn,
) -> tuple[str, MatchFn, FilterFn]:
    """Wrap a (matches, apply) pair with a name for logging."""
    def safe_apply(text: str) -> str:
        try:
            out = apply(text)
            return out if len(out) < len(text) else text
        except Exception as exc:  # noqa: BLE001
            logger.debug("rtk filter %s failed: %s", name, exc)
            return text
    return name, matches, safe_apply


# ---------------------------------------------------------------------------
# git diff
# ---------------------------------------------------------------------------

def _git_diff_matches(text: str) -> bool:
    return "diff --git " in text[:1024]


def _git_diff_apply(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("diff --git "):
            out.append(line)
        elif line.startswith("index "):
            continue
        elif line.startswith("--- "):
            continue
        elif line.startswith("+++ "):
            out.append(line)
        elif line.startswith("@@"):
            out.append(line)
        elif line[0] in "+-":
            out.append(line)
        # else: drop unchanged context
    return "\n".join(out) + ("\n" if out else "")


GIT_DIFF = _make_filter("git-diff", _git_diff_matches, _git_diff_apply)


# ---------------------------------------------------------------------------
# git status
# ---------------------------------------------------------------------------

_GIT_STATUS_STATE_RE = re.compile(
    r"^(?:\s*)(?P<state>(?:modified|new file|deleted|renamed|copied|untracked))"
    r"(?:\s*\([^)]*\))?:\s*(?P<rest>.+)$",
)


def _git_status_matches(text: str) -> bool:
    return text.startswith("On branch ") or text.startswith("HEAD detached")


def _git_status_apply(text: str) -> str:
    groups: dict[str, list[str]] = {}
    header: list[str] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if (
            line.startswith("On branch ")
            or line.startswith("HEAD detached")
            or line.startswith("Your branch ")
            or line.startswith("nothing to commit")
            or line.startswith("Changes to be committed")
            or line.startswith("Changes not staged")
            or line.startswith("Untracked files")
        ):
            header.append(line)
            continue
        m = _GIT_STATUS_STATE_RE.match(line)
        if m:
            state = m.group("state").split("(")[0].strip()
            groups.setdefault(state, []).append(m.group("rest").strip())
        else:
            groups.setdefault("other", []).append(line.strip())
    out: list[str] = list(header)
    for state, paths in groups.items():
        if state == "other" and len(paths) <= 3:
            out.extend(paths)
        else:
            out.append(f"{state}: {len(paths)}")
    return "\n".join(out)


GIT_STATUS = _make_filter("git-status", _git_status_matches, _git_status_apply)


# ---------------------------------------------------------------------------
# git log
# ---------------------------------------------------------------------------

_GIT_LOG_HEAD_RE = re.compile(r"^commit [0-9a-f]{7,}")


def _git_log_matches(text: str) -> bool:
    return bool(_GIT_LOG_HEAD_RE.match(text)) and "diff --git " not in text[:2048]


def _git_log_apply(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("commit "):
            out.append(line.split(" ", 1)[1][:12])
        elif line.startswith("Author:") or line.startswith("Date:"):
            continue
        elif line.startswith("    "):
            out.append(line.strip())
    return "\n".join(out)


GIT_LOG = _make_filter("git-log", _git_log_matches, _git_log_apply)


# ---------------------------------------------------------------------------
# cargo test
# ---------------------------------------------------------------------------


def _cargo_test_matches(text: str) -> bool:
    return "test result:" in text and ("running " in text or "FAILED" in text)


def _cargo_test_apply(text: str) -> str:
    passed, failed = 0, 0
    failures: list[str] = []
    summary = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("test result:"):
            summary = stripped
            m = re.search(r"(\d+) passed", stripped)
            if m:
                passed = int(m.group(1))
            m = re.search(r"(\d+) failed", stripped)
            if m:
                failed = int(m.group(1))
            continue
        if stripped.startswith("test ") and ("FAILED" in stripped or "panic" in stripped.lower()):
            failures.append(stripped.split(" ... ")[0])
        elif "FAILED" in stripped and "failures:" not in stripped:
            failures.append(stripped)
    out: list[str] = []
    if summary:
        out.append(summary)
    if failures:
        out.append("failures:")
        out.extend(failures[:20])
        if len(failures) > 20:
            out.append(f"... ({len(failures) - 20} more)")
    return "\n".join(out) if out else ""


CARGO_TEST = _make_filter("cargo-test", _cargo_test_matches, _cargo_test_apply)


# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------


def _pytest_matches(text: str) -> bool:
    head = text[:1024]
    return (
        "test session starts" in head or "::test_" in head
    ) and ("PASSED" in head or "FAILED" in head)


def _pytest_apply(text: str) -> str:
    passed = 0
    failed: list[str] = []
    summary = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("===") and "passed" in stripped.lower():
            summary = stripped
            m = re.search(r"(\d+) passed", stripped)
            if m:
                passed = int(m.group(1))
            continue
        if stripped.startswith("FAILED "):
            failed.append(stripped.split(" - ")[0])
        elif "FAILED" in stripped and "::" in stripped:
            failed.append(stripped.split(" ")[0])
    out: list[str] = []
    if summary:
        out.append(summary)
    if failed:
        out.append("failures:")
        out.extend(failed[:20])
        if len(failed) > 20:
            out.append(f"... ({len(failed) - 20} more)")
    return "\n".join(out) if out else ""


PYTEST = _make_filter("pytest", _pytest_matches, _pytest_apply)


# ---------------------------------------------------------------------------
# ls -la
# ---------------------------------------------------------------------------


def _ls_matches(text: str) -> bool:
    if not text.startswith("total "):
        return False
    nl = text.find("\n")
    if nl == -1:
        return False
    return text[nl + 1 : nl + 11].startswith(("drwx", "-rwx", "-rw-", "lrwx"))


def _ls_apply(text: str) -> str:
    groups: Counter[str] = Counter()
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        name = parts[8]
        top = name.split("/", 1)[0] if "/" in name else "."
        groups[top] += 1
    if not groups:
        return ""
    lines = [f"total: {sum(groups.values())}"]
    for top, count in sorted(groups.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {top}/: {count}")
    return "\n".join(lines)


LS = _make_filter("ls", _ls_matches, _ls_apply)


# ---------------------------------------------------------------------------
# grep / rg
# ---------------------------------------------------------------------------

_GREP_FILE_LINE_RE = re.compile(r"^([^:\s][^:]*?):(\d+)(?::|\s)", re.MULTILINE)


def _grep_matches(text: str) -> bool:
    if not text:
        return False
    return len(_GREP_FILE_LINE_RE.findall(text[:2048])) >= 3


def _grep_apply(text: str) -> str:
    files: "OrderedDict[str, list[str]]" = OrderedDict()
    max_line = 240
    for line in text.splitlines():
        m = _GREP_FILE_LINE_RE.match(line)
        if not m:
            continue
        path = m.group(1)
        truncated = line if len(line) <= max_line else line[: max_line - 1] + "…"
        files.setdefault(path, []).append(truncated)
    if not files:
        return ""
    out: list[str] = []
    total = 0
    for path, hits in files.items():
        total += len(hits)
        # Only inline the actual match lines for files with very few hits.
        # For files with many hits, just report the count — much shorter
        # than repeating the original line text.
        if len(hits) <= 2:
            out.append(f"{path}: {len(hits)}")
            out.extend(f"  {h}" for h in hits)
        else:
            out.append(f"{path}: {len(hits)}")
    out.append(f"total matches: {total}")
    return "\n".join(out)


GREP = _make_filter("grep", _grep_matches, _grep_apply)


# ---------------------------------------------------------------------------
# Generic dedup fallback
# ---------------------------------------------------------------------------


def _dedup_matches(text: str) -> bool:
    if len(text) < 200:
        return False
    lines = text.splitlines()
    if len(lines) < 10:
        return False
    repeats = sum(
        1 for i in range(1, len(lines))
        if lines[i] == lines[i - 1] and lines[i].strip()
    )
    return repeats >= 5


def _dedup_apply(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    run_start = 0
    i = 1
    while i < len(lines):
        if lines[i] == lines[run_start] and lines[i].strip():
            i += 1
            continue
        run_len = i - run_start
        if run_len > 1:
            out.append(f"<{lines[run_start]}> x {run_len}")
        else:
            out.append(lines[run_start])
        run_start = i
        i += 1
    # Flush final run.
    run_len = len(lines) - run_start
    if run_len > 1:
        out.append(f"<{lines[run_start]}> x {run_len}")
    elif run_len == 1:
        out.append(lines[run_start])
    return "\n".join(out)


DEDUP = _make_filter("dedup", _dedup_matches, _dedup_apply)


# ---------------------------------------------------------------------------
# Filter pipeline
# ---------------------------------------------------------------------------

# Order matters: more specific patterns first so we don't misclassify.
_FILTERS: tuple[tuple[str, MatchFn, FilterFn], ...] = (
    GIT_DIFF,
    GIT_STATUS,
    GIT_LOG,
    CARGO_TEST,
    PYTEST,
    LS,
    GREP,
    DEDUP,
)

# Cap on individual content blocks to compress. Larger blocks are
# unlikely to be tool output (they're system prompts or documents) and
# compressing them risks losing important context.
_MAX_BLOCK_BYTES = 64 * 1024
# If the filter output isn't at least this much shorter, skip it.
_MIN_SAVINGS_RATIO = 0.10


def compress_text(text: str) -> str:
    """Apply the best-matching filter to ``text`` and return the result.

    Returns the input unchanged if it's already short or if no filter
    produces meaningful savings (≥10% reduction).
    """
    if not text or len(text) < 200:
        return text
    if len(text) > _MAX_BLOCK_BYTES:
        return text

    original_len = len(text)
    best = text
    for _, _matches, apply in _FILTERS:
        if not _matches(text):
            continue
        candidate = apply(text)
        if len(candidate) < len(best):
            best = candidate
        # If we already beat the savings threshold, stop early.
        if len(best) < original_len * (1 - _MIN_SAVINGS_RATIO):
            break

    return best if len(best) < original_len else text


def compress_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compress long ``tool_result`` content blocks in an OpenAI messages list.

    Operates on a copy (the input list is not mutated) and only touches
    messages whose role is ``tool`` with string content. Any exception
    is logged at debug level and the original messages are returned.
    """
    if not _ENABLED or not messages:
        return messages
    try:
        out: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                out.append(msg)
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                out.append(msg)
                continue
            compressed = compress_text(content)
            if compressed is content:
                out.append(msg)
            else:
                out.append({**msg, "content": compressed})
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("rtk compress_tool_results failed: %s", exc)
        return messages


__all__ = [
    "is_enabled",
    "set_enabled",
    "compress_text",
    "compress_tool_results",
]
