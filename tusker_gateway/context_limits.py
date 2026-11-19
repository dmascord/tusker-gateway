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
prompt + output stays inside the window. Conservative built-in defaults
apply when no explicit override exists, so undeclared cloud routes are
still protected from silent upstream truncation; explicit overrides always
win.

Learned windows are persisted in the active state backend so runtime signals
survive restarts.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
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


# Conservative built-in windows for routes commonly left undeclared in env.
# Undeclared routes were neither filtered nor clamped, so prompts exceeding
# the real upstream window were forwarded intact and providers silently
# truncated earlier messages (166k+ prompts observed with window=undeclared).
# Env overrides always win; model-specific hints can raise provider defaults.
_BUILTIN_PROVIDER_CONTEXT_WINDOWS: dict[str, int] = {
    "openai": 128_000,
    "openai-codex": 128_000,
    "github-copilot": 128_000,
    "azure-openai": 128_000,
    "apim": 128_000,
    "anthropic": 200_000,
    "claude": 200_000,
    "google": 1_048_576,
    "gemini": 1_048_576,
    "vertexai": 1_048_576,
    "opencode-go": 128_000,
    "minimax": 128_000,
    "moonshot": 128_000,
    "kimi": 128_000,
    "deepseek": 128_000,
    "alibaba": 131_072,
    "dashscope": 131_072,
    "qwen": 131_072,
    "xai": 131_072,
    "grok": 131_072,
    "together": 128_000,
    "groq": 128_000,
    "cerebras": 128_000,
    "fireworks": 128_000,
    "sambanova": 128_000,
    "openrouter": 128_000,
    "nvidia": 128_000,
    "nebius": 128_000,
    "sber": 128_000,
}

_BUILTIN_MODEL_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("gpt-4", 128_000),
    ("gpt-5", 128_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
    ("claude", 200_000),
    ("gemini-1.5", 1_048_576),
    ("gemini-2", 1_048_576),
    ("gemini-3", 1_048_576),
    ("glm-4", 128_000),
    ("glm-5", 202_752),
    ("minimax", 1_000_000),
    ("kimi", 262_144),
    ("moonshot", 262_144),
    ("deepseek", 128_000),
    ("qwen", 131_072),
)

# Published windows come from provider catalogs/model metadata; learned
# windows come from observed runtime signals (e.g. response headers or
# context-limit errors). Both are preferred over operator overrides, which
# act as a backup when the upstream/model does not tell us its window.
_published_context_windows: dict[str, int] = {}
_learned_context_windows: dict[str, int] = {}
_learned_context_window_store: "_LearnedContextWindowStore | None" = None
_learned_context_window_store_path: str | None = None
_learned_context_window_lock = threading.RLock()


class _LearnedContextWindowStore:
    """Persist learned context windows in the active storage backend."""

    def __init__(self, path: str):
        from tusker_gateway.storage import shared_database

        self._path = str(path)
        self._db = shared_database(self._path, timeout=5.0, fallback_policy="advisory")
        if not self._db.is_postgres and self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_db()

    def _ensure_db(self) -> None:
        with self._db.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learned_context_windows (
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    "window" INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (provider, model)
                )
                """
            )
            conn.commit()

    def load(self) -> dict[str, int]:
        import time

        with self._db.connection() as conn:
            rows = conn.execute(
                'SELECT provider, model, "window" FROM learned_context_windows'
            ).fetchall()
        out: dict[str, int] = {}
        for row in rows or []:
            try:
                provider = str(row[0]).strip().lower().replace("_", "-")
                model = str(row[1]).strip()
                window = int(row[2])
            except Exception:
                continue
            if provider and model and window > 0:
                out[_route_model_key(provider, model)] = window
        return out

    def upsert(self, provider: str, model: str, window: int, source: str) -> None:
        import time

        with self._db.connection() as conn:
            conn.execute(
                """
                INSERT INTO learned_context_windows
                    (provider, model, "window", source, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    "window"=excluded."window",
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (
                    str(provider).strip().lower().replace("_", "-"),
                    str(model).strip(),
                    int(window),
                    str(source or "unknown"),
                    float(time.time()),
                ),
            )
            conn.commit()


def default_learned_context_window_db_path() -> str:
    configured = os.environ.get("TUSKER_LEARNED_CONTEXT_WINDOW_DB_PATH", "").strip()
    if configured:
        return configured
    quality = os.environ.get("QUALITY_DB_PATH", "").strip()
    if quality and quality != ":memory:":
        return str(Path(quality).with_name("learned_context_windows.db"))
    home = os.environ.get("HOME")
    if home and os.path.isdir(os.path.join(home, ".hermes")):
        return os.path.join(home, ".hermes", "learned_context_windows.db")
    return "data/learned_context_windows.db"


def configure_learned_context_window_store(path: str | None = None) -> None:
    """Load and enable persistence for learned context windows."""
    global _learned_context_window_store, _learned_context_window_store_path
    db_path = str(path or default_learned_context_window_db_path())
    with _learned_context_window_lock:
        if (
            _learned_context_window_store is not None
            and _learned_context_window_store_path == db_path
        ):
            return
        try:
            store = _LearnedContextWindowStore(db_path)
            loaded = store.load()
        except Exception:
            logger.warning(
                "learned context window store unavailable path=%s", db_path, exc_info=True
            )
            return
        _learned_context_window_store = store
        _learned_context_window_store_path = db_path
        for key, value in loaded.items():
            current = _learned_context_windows.get(key)
            if current is None or value >= current:
                _learned_context_windows[key] = value
        if loaded:
            logger.info("loaded %d learned context windows from %s", len(loaded), db_path)


def _route_model_key(provider: str, model: str) -> str:
    return _route_key(f"{provider}/{model}")


def set_published_context_windows(windows: dict[tuple[str, str], int] | None) -> None:
    """Replace the published context-window registry (catalog-sourced)."""
    global _published_context_windows
    parsed: dict[str, int] = {}
    for (provider, model), value in (windows or {}).items():
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n > 0:
            parsed[_route_model_key(str(provider), str(model))] = n
    _published_context_windows = parsed


def update_published_context_windows(windows: dict[tuple[str, str], int] | None) -> None:
    """Merge published context windows into the registry."""
    for (provider, model), value in (windows or {}).items():
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n > 0:
            _published_context_windows[_route_model_key(str(provider), str(model))] = n


def record_learned_context_window(provider: str, model: str, window: int, *, source: str = "unknown") -> None:
    """Record an observed context window for a route and persist it."""
    try:
        n = int(window)
    except (TypeError, ValueError):
        return
    if n <= 0:
        return
    key = _route_model_key(provider, model)
    changed = False
    with _learned_context_window_lock:
        current = _learned_context_windows.get(key)
        # Prefer larger learned values (a header/error usually states the true
        # limit); if equal, keep existing.
        if current is None or n >= current:
            _learned_context_windows[key] = n
            changed = True
            store = _learned_context_window_store
        else:
            store = None
    if not changed:
        return
    if store is not None:
        try:
            store.upsert(provider, model, n, source)
        except Exception:
            logger.warning(
                "failed to persist learned context window provider=%s model=%s",
                provider,
                model,
                exc_info=True,
            )
    logger.info(
        "learned context window provider=%s model=%s window=%d source=%s",
        provider,
        model,
        n,
        source,
    )


def _lookup_known_window(mapping: dict[str, int], provider: str, model: str) -> int | None:
    provider_key = _route_key(provider)
    model_key = _route_key(model)
    exact = mapping.get(_route_model_key(provider, model))
    if exact is not None:
        return exact
    # Accept lookups where either side stored/queried an implicit :latest tag.
    prefix = provider_key + "/"
    for key, value in mapping.items():
        if not key.startswith(prefix):
            continue
        key_model = key[len(prefix):]
        if key_model == model_key or key_model.removesuffix(":latest") == model_key.removesuffix(":latest"):
            return value
    return None


_CONTEXT_HEADER_KEYS = (
    "x-context-length",
    "x-context-window",
    "x-max-context-length",
    "x-model-context-length",
    "x-model-max-context",
    "x-model-max-tokens",
    "openai-model-context-length",
)


def learn_from_response_headers(provider: str, model: str, headers: Any) -> int | None:
    """Record context window hints from upstream response headers, if present."""
    if not headers:
        return None
    try:
        items = headers.items() if hasattr(headers, "items") else []
    except Exception:
        return None
    for key, value in items:
        k = str(key).strip().lower()
        if k in _CONTEXT_HEADER_KEYS or ("context" in k and ("length" in k or "window" in k or "token" in k)):
            # Ignore obvious rate-limit headers.
            if "ratelimit" in k or "remaining" in k or "reset" in k:
                continue
            try:
                n = int(str(value).strip())
            except (TypeError, ValueError):
                continue
            if n >= 1024:
                record_learned_context_window(provider, model, n, source=f"header:{k}")
                return n
    return None


import re as _re

_CONTEXT_ERROR_RE = _re.compile(
    r"(?i)(?:maximum|max(?:imum)?)?\s*context(?:\s+length|\s+window|\s+size)?(?:\s+is|\s+of|[:=])\s*(\d[\d,]{3,})"
    r"|context(?:\s+length|\s+window)\s*(?:exceeded|too\s+(?:large|long)).{0,80}?(\d[\d,]{3,})"
)


def learn_from_error(provider: str, model: str, message: Any) -> int | None:
    """Record context window hints from provider error text, if present."""
    text = str(message or "")
    if not text:
        return None
    for m in _CONTEXT_ERROR_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        if not raw:
            continue
        try:
            n = int(raw.replace(",", ""))
        except (TypeError, ValueError):
            continue
        # Heuristic: context limits are typically >= 4096 tokens.
        if n >= 4096:
            record_learned_context_window(provider, model, n, source="error")
            return n
    return None


def known_context_window(provider: str, model: str) -> int | None:
    """Published or learned window for a route, if known."""
    learned = _lookup_known_window(_learned_context_windows, provider, model)
    published = _lookup_known_window(_published_context_windows, provider, model)
    if learned is not None and published is not None:
        return max(learned, published)
    return learned if learned is not None else published


def _builtin_context_window(provider: str, model: str) -> int | None:
    pkey = _route_key(provider)
    # Command Code models have internal thinking_budgets that exceed typical
    # context windows; a low fitted max_tokens causes 400s such as
    # "max_completion_tokens [1024] must be greater than thinking_budget [24576]".
    # Return a generous built-in value instead of falling back to a scarce one.
    if pkey == "commandcode":
        return (
            int(os.environ.get("TUSKER_CONTEXT_WINDOW_COMMANDCODE", "65536"))
            if _env_int("TUSKER_CONTEXT_WINDOW_COMMANDCODE", 0) != 0
            else 65536
        )
    value = _BUILTIN_PROVIDER_CONTEXT_WINDOWS.get(pkey)
    mkey = _route_key(model)
    for prefix, window in _BUILTIN_MODEL_CONTEXT_WINDOWS:
        prefix_key = _route_key(prefix)
        if mkey == prefix_key or mkey.startswith(prefix_key + "-") or mkey.startswith(prefix_key + "."):
            if value is None or window > value:
                value = window
            break
    return value


def declared_context_window(provider: str, model: str) -> int | None:
    """Return the effective context window for a route.

    Resolution order:
    1. Published/catalog or learned (observed) window, when known.
    2. Operator override (``TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON``) as a
       backup when the upstream/model does not tell us its window.
    3. Conservative built-in default.
    """
    known = known_context_window(provider, model)
    if known is not None:
        return known
    overrides = _context_window_overrides()
    provider_key = _route_key(provider)
    exact = overrides.get(_route_key(f"{provider}/{model}"))
    if exact is not None:
        return exact
    provider_default = overrides.get(provider_key)
    if provider_default is not None:
        return provider_default
    return _builtin_context_window(provider, model)


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
