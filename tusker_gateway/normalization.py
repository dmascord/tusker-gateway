"""Per-client response normalization profile resolver.

Phase 0 of the UA-driven response-normalization feature
(`docs/investigation-2026-10-16-ua-normalization.md`). This module is the
single source of truth for:

* The closed set of :class:`ClientProfile` values the gateway can assign.
* A canonical 64-hex SHA-256 caller fingerprint and the 32/16/12-hex
  short forms derived from it. Existing tables key on their legacy
  widths and are not migrated; the 64-hex value is propagated only via
  the access log and span attribute.
* The :func:`resolve_profile` resolver, which honors the locked
  precedence: explicit ``X-Tusker-Harness`` / ``X-Client-Harness`` /
  body ``harness`` / ``metadata.harness`` header > ``User-Agent``
  substring > first-message fingerprint tie-breaker > ``Authorization``
  key fingerprint.
* A :class:`ContextVar` for the per-request profile, mirroring
  :mod:`tusker_gateway.question_adapters` so handlers and middleware
  can read the profile without threading arguments.
* The ``TUSKER_NORMALIZATION_SHADOW`` env flag, which gates whether
  Phase 0 actually does anything observable. Default ``1`` for the
  shadow release; the flag exists so the operator can opt the whole
  feature off at process start without code changes.
* A per-profile request counter, which the access log reads and
  ``/status`` exposes.

Phase 0 makes no response-shape change. It only:

* computes the profile and stashes it on ``request["client_profile"]``
  and the context var;
* emits it in the access log and on the chat / anthropic root span;
* counts requests per profile, exposed via ``/status``;
* exposes a :func:`record_shadow_mismatch` hook the future error
  dispatcher (Phase 1) will call when the shadow shape would have
  differed from the emitted shape. Phase 0 itself does not call it.

Behavior contract (must remain stable across phases):

* UA alone never downgrades any safety posture; explicit header > body
  > UA > raw key fingerprint.
* Every request has a profile (``default`` is the explicit catch-all).
* The 64-hex fingerprint is deterministic from the API key, so two
  requests with the same key produce the same ``key_fingerprint_64``.
* The first-message fingerprint is consulted **only** as a tie-breaker
  between ``default-openai`` and ``default`` for ambiguous UAs; it is
  not a security boundary.
"""
from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ClientProfile(str, Enum):
    """Closed set of per-client normalization profiles.

    String-valued so it survives JSON serialization (access log, /status)
    without an ``Enum.value`` call at every emission site.
    """

    OMP = "omp"
    OPENCODE = "opencode"
    CLAUDE_CODE = "claude-code"
    KILO = "kilo"
    CODEX_CLI = "codex_cli"
    DEFAULT_OPENAI = "default-openai"
    DEFAULT = "default"


# Detection substrings, ordered most-specific first. Order is significant
# only as a defensive tie-breaker; the explicit-header path always wins.
_UA_RULES: tuple[tuple[ClientProfile, tuple[str, ...]], ...] = (
    (ClientProfile.OPENCODE, ("opencode", "open-code")),
    (ClientProfile.CLAUDE_CODE, ("claude-code", "claude_code")),
    (ClientProfile.KILO, ("kilo",)),
    (ClientProfile.CODEX_CLI, ("codex",)),
    (ClientProfile.OMP, ("omp",)),
    (
        ClientProfile.DEFAULT_OPENAI,
        (
            "openai/",  # OpenAI Python SDK "OpenAI/Python 1.x.y"
            "openai-",
            "python-requests",
            "python-httpx",
            "httpx",
            "curl/",
            "postman",
            "insomnia",
        ),
    ),
)


# UA strings that should fall through to ``default`` even if they contain
# one of the default-openai substrings. ``python-requests`` is common
# enough as a generic HTTP client that we want to be conservative about
# claiming the caller is a strict OpenAI-shape consumer; the first-message
# tie-breaker is what promotes it to ``default-openai``.
_AMBIGUOUS_UA_HINTS: tuple[str, ...] = (
    "curl/",
    "python-requests",
    "httpx",
)


def fingerprint_api_key_64(api_key: str) -> str:
    """Return the canonical 64-hex SHA-256 fingerprint of an API key.

    Distinct from :func:`tusker_gateway.identity.fingerprint_api_key`
    (which also returns a 64-hex SHA-256 of the key bytes) so that any
    future change in canonicalization does not silently propagate to
    every call site. Today the two functions are byte-equivalent on
    ASCII input.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _user_agent_substring(ua: str) -> ClientProfile | None:
    """Return the first matching profile for *ua*, or ``None``.

    Skips the ``default-openai`` rule for ambiguous UAs (curl, bare
    python-requests, httpx) so the first-message tie-breaker in
    :func:`resolve_profile` can run instead. This matches the locked
    precedence: rule 5 fires only when the UA is a clear OpenAI-style
    consumer.
    """
    lowered = ua.lower()
    ambiguous = any(hint in lowered for hint in _AMBIGUOUS_UA_HINTS)
    for profile, markers in _UA_RULES:
        if ambiguous and profile is ClientProfile.DEFAULT_OPENAI:
            continue
        for marker in markers:
            if marker in lowered:
                return profile
    return None


def fingerprint_short(
    fingerprint_64: str, *, width: int
) -> str:
    """Return a derived short form of the 64-hex fingerprint.

    ``width`` must be one of 32, 16, 12. Widths match the legacy
    call sites in the codebase: ``rate_limit.py`` (32), trace attributes
    (16), and the synthetic log principal (12). No DB migration is
    required - the budget, rate-limit, idempotency, and cache tables
    keep their existing column widths.
    """
    if width not in (32, 16, 12):
        raise ValueError(f"unsupported fingerprint width: {width!r}")
    return fingerprint_64[:width]


@dataclass(frozen=True)
class ResolvedProfile:
    """A resolved per-request profile plus the fields needed to log it.

    Carrying the raw inputs (``user_agent``, ``key_fingerprint_64``)
    alongside the chosen profile lets the access log and span attribute
    record the diagnostic fields once at resolution time, instead of
    re-reading headers in the access-log path (which would need the
    request object).
    """

    profile: ClientProfile
    detection_source: str  # "header" | "body" | "ua" | "first_message" | "default"
    user_agent: str
    key_fingerprint_64: str
    key_fingerprint_short: str  # 16-hex for span attribute / log principal

    def access_log_fields(self) -> dict[str, str]:
        return {
            "client_profile": self.profile.value,
            "client_profile_source": self.detection_source,
            "key_fingerprint_64": self.key_fingerprint_64,
        }


_CURRENT: ContextVar[ResolvedProfile | None] = ContextVar(
    "tusker_client_profile", default=None
)


def set_current(resolved: ResolvedProfile) -> None:
    """Bind the resolved profile to the current request's context."""
    _CURRENT.set(resolved)


def current() -> ResolvedProfile | None:
    """Return the profile bound to the current request, or ``None``."""
    return _CURRENT.get()


def _header_value(headers: Any, name: str) -> str | None:
    """Read a header from an aiohttp request, a ``Mapping``, or ``None``."""
    if headers is None:
        return None
    if hasattr(headers, "get"):
        value = headers.get(name)
    else:
        return None
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - defensive; never fail resolution
            return None
    text = str(value).strip()
    return text or None


def _looks_like_chat_first_message(first_message: str) -> bool:
    """Return True if the first user message looks like a chat question.

    Used as the tie-breaker between ``default-openai`` and ``default``
    for callers with empty or generic UAs (curl, python-requests, ...).
    Not a security boundary.
    """
    if not isinstance(first_message, str):
        return False
    text = first_message.strip()
    if len(text) < 4 or len(text) > 4000:
        return False
    if "{" in text and "}" in text and '"' in text:
        # Embedded JSON Schema / JSON-RPC / tool-call payloads are not
        # a chat question.
        return False
    if text.endswith("?") or text.endswith("?"):
        return True
    # Sentence punctuation: period, exclamation, or a multi-clause
    # utterance (contains a comma at least once).
    if text.endswith((".", "!")):
        return True
    return False


def _first_user_message(body: Mapping[str, Any] | None) -> str | None:
    """Return the first user-role message text from a chat body, if any."""
    if not isinstance(body, Mapping):
        return None
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Concatenate plain text parts only; leave tool/JSON parts alone.
            parts: list[str] = []
            for part in content:
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            if parts:
                return " ".join(parts)
        return None
    return None


def resolve_profile(
    *,
    headers: Any = None,
    body: Mapping[str, Any] | None = None,
    api_key: str | None = None,
) -> ResolvedProfile:
    """Resolve a per-request :class:`ClientProfile`.

    Precedence (locked in
    ``docs/investigation-2026-10-16-ua-normalization.md`` §6):

    1. ``X-Tusker-Harness`` header
    2. ``X-Client-Harness`` header
    3. ``body.harness`` (request body)
    4. ``body.metadata.harness``
    5. ``User-Agent`` substring
    6. First-message fingerprint tie-breaker (only between
       ``default-openai`` and ``default``)
    7. ``Authorization`` key fingerprint (only used to derive the
       fingerprint field; the profile itself is ``default``)
    """
    user_agent = _header_value(headers, "User-Agent") or ""
    key_fingerprint_64 = (
        fingerprint_api_key_64(api_key) if api_key else ""
    )
    key_fingerprint_short = (
        fingerprint_short(key_fingerprint_64, width=16)
        if key_fingerprint_64
        else ""
    )

    # 1, 2, 3, 4: explicit harness hints.
    for source, name in (
        ("header", "X-Tusker-Harness"),
        ("header", "X-Client-Harness"),
    ):
        value = _header_value(headers, name)
        if value and (profile := _user_agent_substring(value)) is not None:
            return ResolvedProfile(
                profile=profile,
                detection_source=source,
                user_agent=user_agent,
                key_fingerprint_64=key_fingerprint_64,
                key_fingerprint_short=key_fingerprint_short,
            )

    if isinstance(body, Mapping):
        harness = body.get("harness")
        if isinstance(harness, str) and (
            profile := _user_agent_substring(harness)
        ) is not None:
            return ResolvedProfile(
                profile=profile,
                detection_source="body",
                user_agent=user_agent,
                key_fingerprint_64=key_fingerprint_64,
                key_fingerprint_short=key_fingerprint_short,
            )
        metadata = body.get("metadata")
        if isinstance(metadata, Mapping):
            harness = metadata.get("harness")
            if isinstance(harness, str) and (
                profile := _user_agent_substring(harness)
            ) is not None:
                return ResolvedProfile(
                    profile=profile,
                    detection_source="body",
                    user_agent=user_agent,
                    key_fingerprint_64=key_fingerprint_64,
                    key_fingerprint_short=key_fingerprint_short,
                )

    # 5: User-Agent substring.
    if user_agent:
        profile = _user_agent_substring(user_agent)
        if profile is not None:
            return ResolvedProfile(
                profile=profile,
                detection_source="ua",
                user_agent=user_agent,
                key_fingerprint_64=key_fingerprint_64,
                key_fingerprint_short=key_fingerprint_short,
            )

    # 6: first-message tie-breaker for ambiguous UAs.
    if any(hint in user_agent.lower() for hint in _AMBIGUOUS_UA_HINTS):
        first = _first_user_message(body)
        if first and _looks_like_chat_first_message(first):
            return ResolvedProfile(
                profile=ClientProfile.DEFAULT_OPENAI,
                detection_source="first_message",
                user_agent=user_agent,
                key_fingerprint_64=key_fingerprint_64,
                key_fingerprint_short=key_fingerprint_short,
            )

    # 7: catch-all.
    return ResolvedProfile(
        profile=ClientProfile.DEFAULT,
        detection_source="default",
        user_agent=user_agent,
        key_fingerprint_64=key_fingerprint_64,
        key_fingerprint_short=key_fingerprint_short,
    )


# --- Shadow / metrics ----------------------------------------------------

def shadow_mode_enabled() -> bool:
    """Return whether the normalization shadow release is active.

    Reads ``TUSKER_NORMALIZATION_SHADOW`` per request (cheap env-var
    lookup; no startup caching so an operator can flip it without a
    restart). The default is ``"1"`` (shadow active) so the very first
    release of Phase 0 ships in shadow mode and produces only log
    evidence.
    """
    raw = os.environ.get("TUSKER_NORMALIZATION_SHADOW", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


# Per-profile request counter. Kept module-local (no app-state
# dependency) so a future /status block can read it directly without
# threading a registry through the app.
_PROFILE_COUNTS: dict[str, int] = {p.value: 0 for p in ClientProfile}
_SHADOW_MISMATCH_COUNTS: dict[tuple[str, str], int] = {}


def record_resolved(resolved: ResolvedProfile) -> None:
    """Increment the per-profile request counter for one resolved profile.

    Idempotent across calls within a request: callers may invoke this
    from both the auth middleware (on first authentication) and the
    access-log path (on completion); the second call within a request
    must be a no-op so the count is per-request, not per-event.
    """
    _PROFILE_COUNTS[resolved.profile.value] = (
        _PROFILE_COUNTS.get(resolved.profile.value, 0) + 1
    )


def record_shadow_mismatch(profile: ClientProfile, surface: str) -> None:
    """Increment the ``tusker.shadow_shape_mismatch_total`` counter.

    Phase 1 (the error-envelope dispatcher) will call this when the
    shadow shape would have differed from the emitted shape. Phase 0
    does not call it; the helper exists now so the metric wiring
    (registry, render) can land with Phase 0 without a Phase 1
    follow-up.
    """
    key = (profile.value, surface)
    _SHADOW_MISMATCH_COUNTS[key] = _SHADOW_MISMATCH_COUNTS.get(key, 0) + 1


def profile_counts_snapshot() -> dict[str, int]:
    """Return a copy of the per-profile request counters."""
    return dict(_PROFILE_COUNTS)


def shadow_mismatch_snapshot() -> dict[tuple[str, str], int]:
    """Return a copy of the per-(profile, surface) shadow-mismatch counters."""
    return dict(_SHADOW_MISMATCH_COUNTS)


def reset_counters_for_tests() -> None:
    """Clear the in-process counters. Test-only; not exported."""
    _PROFILE_COUNTS.clear()
    _SHADOW_MISMATCH_COUNTS.clear()
    for p in ClientProfile:
        _PROFILE_COUNTS[p.value] = 0


__all__ = [
    "ClientProfile",
    "ResolvedProfile",
    "fingerprint_api_key_64",
    "fingerprint_short",
    "resolve_profile",
    "set_current",
    "current",
    "shadow_mode_enabled",
    "record_resolved",
    "record_shadow_mismatch",
    "profile_counts_snapshot",
    "shadow_mismatch_snapshot",
    "reset_counters_for_tests",
]
