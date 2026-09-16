"""Process-local cache for Gemini ``thought_signature`` values.

Gemini's OpenAI-compatibility endpoint returns a ``thought_signature`` inside
``tool_calls[i].extra_content.google.thought_signature`` on every assistant
message that produced tool calls. The signature is opaque to OpenAI-format
clients (LiteLLM, OpenCode, etc.) that drop unknown fields when echoing the
assistant message back, so the next request arrives without it and Gemini
rejects the turn with::

    "Function call is missing a thought_signature in functionCall parts.
     This is required for tools to work correctly..."

The cache keys signatures by the ``tool_call_id`` Gemini assigned (e.g.
``call_118565``). On the request path, the gateway walks the chat transcript
and injects any cached signature into assistant ``tool_calls`` entries that
are missing ``extra_content.google.thought_signature``. On the response path
(non-streaming JSON and streaming SSE), it stores every signature it sees.

Design notes:
- Pure in-memory, process-local. A pod restart drops the cache; the
  subsequent turn degrades to the same 400 the client already saw, then
  recovers on the next full round trip.
- Bounded by ``MAX_ENTRIES`` (LRU-evicted) and ``TTL_SECS``.
- Thread-safe via a single ``threading.Lock``. The critical sections are
  O(N) over tool_calls in one response which is tiny (single digits).
"""

from __future__ import annotations

import threading
import time
from typing import Any


MAX_ENTRIES = 10_000
TTL_SECS = 3600.0

# Allowed location of the signature in an OpenAI-format tool_call object.
_SIG_CONTAINER_KEYS = ("extra_content", "google", "thought_signature")


class GeminiThoughtCache:
    """Process-local map of ``tool_call_id`` → ``thought_signature``."""

    def __init__(self, *, max_entries: int = MAX_ENTRIES, ttl_secs: float = TTL_SECS) -> None:
        self._max_entries = max_entries
        self._ttl_secs = ttl_secs
        self._lock = threading.Lock()
        # Maps call_id -> (signature, expires_at_monotonic).
        self._entries: dict[str, tuple[str, float]] = {}

    # -- Store --------------------------------------------------------

    def store(self, call_id: str, signature: str) -> None:
        """Record ``signature`` for ``call_id`` until TTL elapses."""
        if not call_id or not signature:
            return
        expires = time.monotonic() + self._ttl_secs
        with self._lock:
            self._entries[call_id] = (signature, expires)
            self._evict_locked()

    def store_many(self, items: dict[str, str]) -> None:
        for call_id, signature in items.items():
            if call_id and signature:
                self.store(call_id, signature)

    # -- Retrieve -----------------------------------------------------

    def get(self, call_id: str) -> str | None:
        """Return the cached signature for ``call_id`` or ``None`` if absent/expired."""
        if not call_id:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(call_id)
            if entry is None:
                return None
            signature, expires = entry
            if expires < now:
                self._entries.pop(call_id, None)
                return None
            return signature

    # -- Extract from a provider response -----------------------------

    @staticmethod
    def extract_from_response(response: Any) -> dict[str, str]:
        """Pull every ``thought_signature`` out of a parsed response object.

        Handles both the non-streaming shape (``choices[*].message``) and the
        streaming chunk shape (``choices[*].delta``). Returns a ``call_id →
        signature`` dict of new entries only (the caller decides whether to
        store them).
        """
        found: dict[str, str] = {}
        if not isinstance(response, dict):
            return found
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            # Non-streaming final message.
            message = choice.get("message")
            if isinstance(message, dict):
                _collect_from_tool_calls(message.get("tool_calls"), found)
                # Some Gemini variants surface signature at the message level.
                msg_sig = _read_signature_path(message)
                if msg_sig:
                    # Map a message-level signature onto each tool_call_id in
                    # the same message — the signature proves the model
                    # generated the calls, so it applies to all of them.
                    for tc in message.get("tool_calls") or []:
                        if isinstance(tc, dict):
                            cid = tc.get("id")
                            if cid:
                                found[cid] = msg_sig
            # Streaming delta.
            delta = choice.get("delta")
            if isinstance(delta, dict):
                _collect_from_tool_calls(delta.get("tool_calls"), found)
        return found

    def extract_and_store(self, response: Any) -> int:
        """Convenience wrapper: extract and store new entries. Returns count stored."""
        found = self.extract_from_response(response)
        if not found:
            return 0
        self.store_many(found)
        return len(found)

    # -- Inject into a request transcript ----------------------------

    def inject_into_messages(self, messages: list[Any]) -> int:
        """Mutate ``messages`` in place: fill missing signatures from the cache.

        Only touches ``assistant`` messages with ``tool_calls``. Skips tool_calls
        that already carry a non-empty ``extra_content.google.thought_signature``.
        Returns the number of tool_calls that were patched.
        """
        if not messages:
            return 0
        patched = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                continue
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                call_id = tc.get("id")
                if not call_id:
                    continue
                if _read_signature_path(tc):
                    continue
                signature = self.get(call_id)
                if not signature:
                    continue
                _write_signature_path(tc, signature)
                patched += 1
        return patched

    # -- Internals ----------------------------------------------------

    def _evict_locked(self) -> None:
        """Drop expired entries and, if still over budget, oldest by insertion."""
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._entries.items() if exp < now]
        for k in expired:
            self._entries.pop(k, None)
        if len(self._entries) <= self._max_entries:
            return
        # Insertion-ordered dict (Python 3.7+): drop the oldest entries first.
        overflow = len(self._entries) - self._max_entries
        for k in list(self._entries.keys())[:overflow]:
            self._entries.pop(k, None)


def _collect_from_tool_calls(tool_calls: Any, into: dict[str, str]) -> None:
    if not isinstance(tool_calls, list):
        return
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        signature = _read_signature_path(tc)
        if not signature:
            continue
        call_id = tc.get("id")
        if call_id:
            into[call_id] = signature


def _read_signature_path(node: Any) -> str | None:
    """Return the ``thought_signature`` at ``extra_content.google.thought_signature`` if present."""
    if not isinstance(node, dict):
        return None
    cursor: Any = node
    for key in _SIG_CONTAINER_KEYS:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def _write_signature_path(tc: dict[str, Any], signature: str) -> None:
    """Insert the signature at ``extra_content.google.thought_signature``."""
    extra = tc.get("extra_content")
    if not isinstance(extra, dict):
        extra = {}
        tc["extra_content"] = extra
    google = extra.get("google")
    if not isinstance(google, dict):
        google = {}
        extra["google"] = google
    google["thought_signature"] = signature


# Process-global cache; reset on pod restart. Tests construct their own
# instance via ``GeminiThoughtCache()`` to avoid cross-test contamination.
_default_cache: GeminiThoughtCache | None = None
_default_lock = threading.Lock()


def default_cache() -> GeminiThoughtCache:
    global _default_cache
    if _default_cache is None:
        with _default_lock:
            if _default_cache is None:
                _default_cache = GeminiThoughtCache()
    return _default_cache


__all__ = [
    "GeminiThoughtCache",
    "default_cache",
    "MAX_ENTRIES",
    "TTL_SECS",
]
