"""Context-headroom advisory headers.

The gateway already knows two things on every request: an estimate of the
prompt it is about to send (``context_limits.estimate_prompt_tokens``) and,
for a directly-addressable route, the route's declared context window. Before
this module the only way a client learned the prompt was getting large was
the hard ``context_length_exceeded`` failure (``errors.py``), which costs a
full round-trip and leaves agent clients in compaction recovery.

These headers make the same numbers advisory-only and client-visible:

``X-Tusker-Context-Estimate``
    Estimated prompt tokens for this request. Always emitted when known.
``X-Tusker-Context-Window``
    Declared/learned context window of the route that served the request.
``X-Tusker-Context-Remaining``
    ``window - estimate``.
``X-Tusker-Context-Used-Pct``
    ``estimate / window`` as an integer percentage.
``X-Tusker-Context-Warning``
    Present only once usage reaches the advisory threshold; the value is a
    short client-safe sentence.

Advisory only: nothing here changes routing, pricing, token accounting or the
request body. The window triplet is omitted when the route's window is not
known (e.g. a pool alias whose served route was not yet selected) rather than
fabricate one; the bare estimate is still emitted.

``TUSKER_CONTEXT_ADVISORY`` (default ``1``) disables emission entirely when
``0``/``false``/``no``/``off``. ``TUSKER_CONTEXT_ADVISORY_PCT`` (default
``80``) sets the warning threshold.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from tusker_gateway.context_limits import (
    declared_context_window,
    estimate_prompt_tokens,
    required_context_tokens,
)
from tusker_gateway.routing import resolve_route

logger = logging.getLogger(__name__)

HEADER_ESTIMATE = "X-Tusker-Context-Estimate"
HEADER_WINDOW = "X-Tusker-Context-Window"
HEADER_REMAINING = "X-Tusker-Context-Remaining"
HEADER_USED_PCT = "X-Tusker-Context-Used-Pct"
HEADER_WARNING = "X-Tusker-Context-Warning"

_DEFAULT_ADVISORY_PCT = 80


def _advisory_enabled() -> bool:
    return os.environ.get("TUSKER_CONTEXT_ADVISORY", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _advisory_pct() -> int:
    try:
        value = int(os.environ.get("TUSKER_CONTEXT_ADVISORY_PCT", _DEFAULT_ADVISORY_PCT))
    except (TypeError, ValueError):
        return _DEFAULT_ADVISORY_PCT
    return min(100, max(1, value))


def _resolve_window(provider: str | None, model: str | None) -> int | None:
    if not provider or not model:
        return None
    if provider == "unknown" or model == "unknown":
        return None
    try:
        return declared_context_window(provider, model)
    except Exception:  # noqa: BLE001 - advisory only, never fail the request
        logger.debug("context advisory window lookup failed", exc_info=True)
        return None


def _window_for(provider: str | None, model: str | None, model_hint: str | None) -> int | None:
    window = _resolve_window(provider, model)
    if window is not None:
        return window
    if model_hint:
        try:
            route = resolve_route(model_hint, {"model": model_hint})
        except Exception:  # noqa: BLE001
            return None
        if route.kind == "passthrough" and route.provider and route.model:
            return _resolve_window(route.provider, route.model)
    return None


def context_advisory_headers(
    messages: Any,
    tools: list[dict[str, Any]] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    model_hint: str | None = None,
    prompt_estimate: int | None = None,
    config: dict[str, Any] | None = None,  # accepted for caller convenience; unused
    pool_name: str | None = None,  # advisory of the pool fallback is deliberately not emitted
) -> dict[str, str]:
    """Build advisory headers. Returns ``{}`` when nothing can be reported."""
    if not _advisory_enabled():
        return {}
    if prompt_estimate is None:
        try:
            prompt_estimate = estimate_prompt_tokens(messages, tools)
        except Exception:  # noqa: BLE001 - an advisory must never fail a turn
            logger.debug("context advisory estimate failed", exc_info=True)
            return {}
    if prompt_estimate <= 0:
        return {}

    headers = {HEADER_ESTIMATE: str(int(prompt_estimate))}

    window = _window_for(provider, model, model_hint)
    if window is None:
        return headers

    used = min(999, int(prompt_estimate * 100 / window))
    headers[HEADER_WINDOW] = str(int(window))
    headers[HEADER_REMAINING] = str(int(window - prompt_estimate))
    headers[HEADER_USED_PCT] = str(used)

    threshold = _advisory_pct()
    if prompt_estimate * 100 >= window * threshold:
        floor = required_context_tokens(prompt_estimate)
        headers[HEADER_WARNING] = (
            f"prompt is {used}% of the {int(window)}-token context window "
            f"({prompt_estimate} of {int(window)} tokens); a route needs ~{floor} "
            "tokens to answer with the output floor. Compact or start a new "
            "session before the next turn fails."
        )
    return headers


__all__ = [
    "HEADER_ESTIMATE",
    "HEADER_REMAINING",
    "HEADER_USED_PCT",
    "HEADER_WARNING",
    "HEADER_WINDOW",
    "context_advisory_headers",
]
