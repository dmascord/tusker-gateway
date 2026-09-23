"""Effective context windows and per-candidate output budgets.

Pool entries inherit the pool's ``context_window`` (128k by default), but a
local server's real window is whatever it was started with. Ollama picks
``num_ctx`` from host memory (32k on the 48 GB MLX Mac) and ignores
``options.num_ctx`` on ``/v1/chat/completions``, so a 32.7k-token prompt
routed to a "128k" candidate leaves ~100 tokens for output and the stream
ends ``finish_reason=length`` mid-reasoning (req_7b0d460047e30296).

``TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON`` declares the real windows, keyed by
``provider`` or ``provider/model`` (model keys win)::

    {"mlx-mac": 32768, "mlx-mac/ornith-1.5:35b": 65536}

Routes with a declared window are skipped when the prompt estimate plus a
minimum output reserve does not fit, and ``max_tokens`` is clamped so
prompt + output stays inside the window. Routes without one are neither
filtered nor clamped (the pool-wide ``context_window`` is only a guess).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Measured on ornith-1.5:35b (Qwen tokenizer): code ~3.3, tool-schema JSON
# ~3.4, English prose ~5.1 chars/token. Agent traffic is mostly code and
# JSON, and over-estimating is the safe direction.
_CHARS_PER_TOKEN = 3.2
_MESSAGE_OVERHEAD_TOKENS = 8
_DEFAULT_IMAGE_TOKENS = 1600
_DEFAULT_MIN_OUTPUT_TOKENS = 4096
_DEFAULT_SAFETY_TOKENS = 256
_MAX_TOKENS_KEYS = ("max_tokens", "max_completion_tokens")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def min_output_tokens() -> int:
    """Smallest output budget worth sending a request for."""
    return _env_int("TUSKER_MIN_OUTPUT_TOKENS", _DEFAULT_MIN_OUTPUT_TOKENS)


def _context_window_overrides() -> dict[str, int]:
    """Parse ``TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON`` (read per call for hot reload)."""
    raw = os.environ.get("TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("ignoring TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON: invalid JSON")
        return {}
    if not isinstance(data, dict):
        return {}
    parsed: dict[str, int] = {}
    for key, value in data.items():
        try:
            tokens = int(value)
        except (TypeError, ValueError):
            continue
        if tokens > 0:
            parsed[_route_key(str(key))] = tokens
    return parsed


def _route_key(value: str) -> str:
    """Normalize a route key; Ollama's implicit ``:latest`` tag is optional."""
    value = value.strip().lower()
    return value[: -len(":latest")] if value.endswith(":latest") else value


def declared_context_window(provider: str, model: str) -> int | None:
    """Return the operator-declared window for a route, if any."""
    overrides = _context_window_overrides()
    provider_key = _route_key(provider)
    return overrides.get(_route_key(f"{provider}/{model}"), overrides.get(provider_key))


def _content_size(content: Any) -> tuple[int, int]:
    """Return (chars, images) for OpenAI-format message content."""
    if isinstance(content, str):
        return len(content), 0
    if not isinstance(content, list):
        return 0, 0
    chars = 0
    images = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in {"image_url", "input_image", "image"}:
            images += 1
        else:
            chars += len(str(part.get("text", "")))
    return chars, images


def estimate_prompt_tokens(
    messages: Any,
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate prompt tokens including tool schemas, tool calls and images."""
    chars = 0
    images = 0
    tokens = 0
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        tokens += _MESSAGE_OVERHEAD_TOKENS
        message_chars, message_images = _content_size(message.get("content"))
        chars += message_chars
        images += message_images
        if message.get("tool_calls"):
            chars += len(json.dumps(message["tool_calls"], default=str))
    if tools:
        chars += len(json.dumps(tools, default=str))
    image_tokens = _env_int("TUSKER_IMAGE_TOKEN_ESTIMATE", _DEFAULT_IMAGE_TOKENS)
    return tokens + int(chars / _CHARS_PER_TOKEN) + images * image_tokens


def required_context_tokens(prompt_estimate: int) -> int:
    """Window size a candidate needs to answer a prompt of *prompt_estimate*."""
    return prompt_estimate + min_output_tokens() + _DEFAULT_SAFETY_TOKENS


@dataclass(frozen=True)
class OutputBudget:
    """Per-candidate context accounting, logged for every attempt."""

    window: int | None
    prompt_estimate: int
    requested_max_tokens: int | None
    effective_max_tokens: int | None

    @property
    def clamped(self) -> bool:
        return self.effective_max_tokens != self.requested_max_tokens


def _requested_max_tokens(extra_body: dict[str, Any]) -> tuple[str | None, int | None]:
    for key in _MAX_TOKENS_KEYS:
        value = extra_body.get(key)
        if value is None:
            continue
        try:
            return key, int(value)
        except (TypeError, ValueError):
            return key, None
    return None, None


def fit_output_budget(
    extra_body: dict[str, Any] | None,
    provider: str,
    model: str,
    prompt_estimate: int,
) -> tuple[dict[str, Any], OutputBudget]:
    """Clamp ``max_tokens`` so prompt + output fits the route's declared window.

    Pure: returns a new dict when a change is needed. Only routes with a
    declared window are clamped. Pool selection already skips routes that
    cannot fit ``min_output_tokens()``; a direct route is clamped to whatever
    room is left rather than overrunning the window.
    """
    body = extra_body or {}
    key, requested = _requested_max_tokens(body)
    window = declared_context_window(provider, model)
    if window is None or requested is None:
        return body, OutputBudget(window, prompt_estimate, requested, requested)
    available = window - prompt_estimate - _DEFAULT_SAFETY_TOKENS
    effective = max(1, available)
    if requested <= effective:
        return body, OutputBudget(window, prompt_estimate, requested, requested)
    clamped = dict(body)
    clamped[key] = effective
    return clamped, OutputBudget(window, prompt_estimate, requested, effective)
