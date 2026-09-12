"""Automatic max_tokens floor for reasoning / thinking models.

Reasoning models (Qwen3, Qwopus, OpenAI o1/o3, DeepSeek-R1, etc.) consume
output tokens on an internal chain-of-thought before emitting visible
content.  When a client sends ``max_tokens=20`` for a reasoning model the
entire budget is consumed by the hidden thinking phase and the response
comes back empty or truncated.

This module defines a per-model **floor**: if the caller's ``max_tokens``
is below the floor, the gateway raises it to the floor before forwarding
the request upstream.  The floor is only applied to models whose slug
matches a known reasoning pattern.
"""

from __future__ import annotations

import os
import re
from typing import Any


# ---------------------------------------------------------------------------
# Reasoning model detection
# ---------------------------------------------------------------------------

# Slug substrings (case-insensitive) that identify reasoning / thinking models.
_REASONING_SLUGS: tuple[str, ...] = (
    # Qwen family (MLX-native and upstream)
    "qwen3",  # Qwen3-8B, Qwen3-Coder-30B, etc.
    "qwopus",  # MLX-Qwopus3.5-* family (all reasoning)
    # OpenAI reasoning line
    "o1-",  # o1, o1-preview, o1-mini
    "o3-",  # o3, o3-mini
    "o4-",  # o4-mini
    # DeepSeek
    "deepseek-r1",
    "-r1",  # catch-all for R1 variants
    # Anthropic (extended thinking by default)
    "claude-opus",
    # Google (thinking models)
    "gemini-2.5",
    # MiniMax — all current M-series models emit inline <think> blocks before
    # the visible answer; without a floor the thinking budget eats the
    # response budget.
    "minimax",
)

_REASONING_RE = re.compile(
    "|".join(re.escape(s) for s in _REASONING_SLUGS),
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Per-model floor values
# ---------------------------------------------------------------------------

# Floor in tokens.  Reasoning models need enough budget for thinking
# *plus* the actual visible answer.  2000 is a conservative floor that
# accommodates short-to-medium reasoning chains without being wasteful.
# Environment-variable override (TUSKER_REASONING_MAX_TOKENS_FLOOR) lets
# operators tune this without a code change.
REASONING_FLOOR: int = int(os.environ.get("TUSKER_REASONING_MAX_TOKENS_FLOOR", "2000"))

# Non-reasoning models use no floor (value 0 = no enforcement).
# Upstream providers handle their own defaults when max_tokens is absent.
DEFAULT_FLOOR: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_reasoning_model(model: str) -> bool:
    """Return *True* when *model* matches a known reasoning model slug."""
    return bool(_REASONING_RE.search(model))


def max_tokens_floor(model: str) -> int:
    """Return the minimum ``max_tokens`` for *model*.

    Returns ``REASONING_FLOOR`` for reasoning models and ``DEFAULT_FLOOR``
    (0 — no enforcement) for everything else.
    """
    if is_reasoning_model(model):
        return REASONING_FLOOR
    return DEFAULT_FLOOR


def apply_max_tokens_floor(
    extra_body: dict[str, Any] | None,
    provider: str,
    model: str,
) -> dict[str, Any]:
    """Return *extra_body* with ``max_tokens`` bumped to the per-model floor.

    This is a **pure function** — it returns a *new* dict when a change is
    needed, or the original dict unchanged.  It never mutates its input.

    Parameters
    ----------
    extra_body:
        The passthrough fields dict from ``_build_extra_body()``.
    provider:
        Resolved upstream provider name (for logging context only).
    model:
        Resolved upstream model name (for slug matching).
    """
    if extra_body is None:
        extra_body = {}

    floor = max_tokens_floor(model)
    if floor <= 0:
        return extra_body

    current = extra_body.get("max_tokens")
    if current is None or current >= floor:
        return extra_body

    # Caller sent a low value that would exhaust thinking budget.
    bumped = dict(extra_body)
    bumped["max_tokens"] = floor
    import logging

    logging.getLogger(__name__).info(
        "max_tokens floor: %s/%s %s → %d (reasoning model floor)",
        provider,
        model,
        current,
        floor,
    )
    return bumped
