"""Format translator registry.

Borrowed from the 9router pattern (``open-sse/translator/``): format
conversion lives in pure functions that have no knowledge of HTTP, routing,
or pool selection. Each translator module calls ``register_*()`` as a
side-effect when imported; the registry dispatches by format name.

For Tusker we currently support two formats:

- ``openai`` — the canonical shape every backend speaks.
- ``anthropic`` — Claude's Messages API shape, exposed via
  ``POST /v1/messages`` and translated to OpenAI for dispatch.

The registry design supports adding more source/target formats later
(Codex Responses, Gemini envelopes) without touching the HTTP layer.

Usage::

    from tusker_gateway import translators
    openai_body = translators.translate_request("anthropic", anthropic_body)
    anthropic_chunks = translators.translate_response("anthropic", openai_chunk, state)

For streaming, register a streaming translator with a state factory and a
chunk translator::

    translators.register_streaming(
        target="anthropic",
        state_factory=init_anthropic_stream_state,
        chunk_translator=translate_openai_chunk_to_anthropic_sse,
    )
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Format identifiers
# ---------------------------------------------------------------------------

OPENAI: str = "openai"
ANTHROPIC: str = "anthropic"
# Reserved for future translators (Codex Responses, Gemini envelopes).
RESPONSES: str = "openai-responses"


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

# Request translators: source_format -> openai-shaped body.
# Pure functions; no I/O, no logging of payload contents.
_request_translators: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

# Non-streaming response translators: openai-shaped chunk -> list of
# target-shaped chunks. Returns a list because some targets emit multiple
# chunks per OpenAI chunk (Anthropic's message_start + content_block_start +
# ping etc).
_response_translators: dict[str, Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]] = {}

# Streaming state factories: target_format -> fresh state dict.
# The state dict is mutated in place by the chunk translator across calls.
_stream_state_factories: dict[str, Callable[[], dict[str, Any]]] = {}

# Streaming chunk translators: target_format -> (openai_chunk, state) -> list
# of raw SSE bytes ready to flush to the client.
_stream_chunk_translators: dict[str, Callable[[dict[str, Any], dict[str, Any]], list[bytes]]] = {}


def register_request(
    source: str,
    fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Register a request translator.

    ``fn`` takes the source-format body and returns an OpenAI-shaped body.
    Raises ``ValueError`` if a translator is already registered for this
    source format (callers can ``override=True`` to bypass — see
    :func:`register_request_override`).
    """
    if source in _request_translators:
        raise ValueError(
            f"request translator for {source!r} already registered; "
            "use register_request_override to replace it"
        )
    _request_translators[source] = fn
    logger.debug("registered request translator for %s", source)


def register_request_override(
    source: str,
    fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Replace an existing request translator. Use with care; intended for tests."""
    _request_translators[source] = fn
    logger.debug("overrode request translator for %s", source)


def register_response(
    target: str,
    fn: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]],
) -> None:
    """Register a non-streaming response translator.

    ``fn`` takes ``(openai_chunk, state)`` and returns a list of target-shaped
    chunks. ``state`` is ``{}`` for the first call.
    """
    if target in _response_translators:
        raise ValueError(
            f"response translator for {target!r} already registered; "
            "use register_response_override to replace it"
        )
    _response_translators[target] = fn
    logger.debug("registered response translator for %s", target)


def register_response_override(
    target: str,
    fn: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]],
) -> None:
    """Replace an existing response translator. Intended for tests."""
    _response_translators[target] = fn
    logger.debug("overrode response translator for %s", target)


def register_streaming(
    target: str,
    *,
    state_factory: Callable[[], dict[str, Any]],
    chunk_translator: Callable[[dict[str, Any], dict[str, Any]], list[bytes]],
) -> None:
    """Register a streaming translator pair.

    ``state_factory`` returns a fresh per-stream state dict. ``chunk_translator``
    takes ``(openai_chunk, state)`` and returns raw SSE bytes (Anthropic SSE
    frames, etc.) ready to flush.
    """
    if target in _stream_chunk_translators:
        raise ValueError(
            f"streaming translator for {target!r} already registered; "
            "use register_streaming_override to replace it"
        )
    _stream_state_factories[target] = state_factory
    _stream_chunk_translators[target] = chunk_translator
    logger.debug("registered streaming translator for %s", target)


def register_streaming_override(
    target: str,
    *,
    state_factory: Callable[[], dict[str, Any]],
    chunk_translator: Callable[[dict[str, Any], dict[str, Any]], list[bytes]],
) -> None:
    """Replace an existing streaming translator. Intended for tests."""
    _stream_state_factories[target] = state_factory
    _stream_chunk_translators[target] = chunk_translator
    logger.debug("overrode streaming translator for %s", target)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def translate_request(source: str, body: dict[str, Any]) -> dict[str, Any]:
    """Translate a request body to OpenAI format.

    If ``source == OPENAI`` the body is returned as-is (passthrough).
    Otherwise the registered translator for ``source`` is invoked.
    """
    if source == OPENAI:
        return body
    fn = _request_translators.get(source)
    if fn is None:
        raise ValueError(f"no request translator registered for source={source!r}")
    return fn(body)


def translate_response(
    target: str,
    chunk: dict[str, Any],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Translate a single OpenAI response chunk to ``target`` format.

    If ``target == OPENAI`` the chunk is wrapped in a single-element list and
    returned unchanged. Otherwise the registered translator is invoked.
    """
    if target == OPENAI:
        return [chunk]
    fn = _response_translators.get(target)
    if fn is None:
        raise ValueError(f"no response translator registered for target={target!r}")
    return fn(chunk, state)


def init_streaming_state(target: str) -> dict[str, Any]:
    """Create a fresh per-stream state dict for streaming translation to ``target``.

    If ``target`` has no streaming translator registered, returns ``{}`` —
    callers should detect this and skip translation.
    """
    fn = _stream_state_factories.get(target)
    if fn is None:
        return {}
    return fn()


def stream_chunk(
    target: str,
    chunk: dict[str, Any],
    state: dict[str, Any],
) -> list[bytes]:
    """Translate one OpenAI streaming chunk to target-format SSE bytes."""
    if target == OPENAI:
        # Caller should byte-encode the chunk itself; we have no opinion.
        return []
    fn = _stream_chunk_translators.get(target)
    if fn is None:
        return []
    return fn(chunk, state)


def has_streaming(target: str) -> bool:
    """Return True if a streaming translator is registered for ``target``."""
    return target in _stream_chunk_translators


# ---------------------------------------------------------------------------
# Side-effect imports — each translator module calls register_*() on import.
# ---------------------------------------------------------------------------

def _load_builtin_translators() -> None:
    """Import built-in translator modules so they self-register.

    Called once at package import time. New built-in translators are added by
    appending their import here.
    """
    # Anthropic Messages API ↔ OpenAI Chat Completions.
    from tusker_gateway.translators import anthropic  # noqa: F401


_load_builtin_translators()


__all__ = [
    "OPENAI",
    "ANTHROPIC",
    "RESPONSES",
    "register_request",
    "register_request_override",
    "register_response",
    "register_response_override",
    "register_streaming",
    "register_streaming_override",
    "translate_request",
    "translate_response",
    "init_streaming_state",
    "stream_chunk",
    "has_streaming",
]
