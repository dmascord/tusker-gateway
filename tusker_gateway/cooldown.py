"""Provider cooldown tracking.

Tracks per-(provider, model) cooldown windows so requests are not
sent to a provider that is in a retry-throttling state.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import logging

logger = logging.getLogger(__name__)


MAX_COOLDOWN_SECS = 30 * 86400.0 # 30 days

# Providers whose published rate limits are separated by model (or model
# class). A 429 from one model on these providers must not evict the other
# models from the same provider. Providers not listed here retain the more
# conservative provider-wide behavior.
MODEL_SCOPED_COOLDOWN_PROVIDERS = frozenset({
    "openrouter",
    "google",       # Gemini API
    "groq",
    "anthropic",    # supported by custom provider registries
})
# Bodies that indicate a long-lived quota / usage-limit window (hours or
# days), not a transient rate-limit blip. Backing off 60s for these would
# hammer the upstream with pointless probes until the window resets.
_QUOTA_HINTS = (
    "quota", "quota exceeded", "usage limit", "usage_limit",
    "capacity", "insufficient", "out of credits", "out of quota",
    "billing", "subscription limit", "limit reached", "monthly limit",
    "daily limit", "budget exhausted",
)

# Non-429 permanent provider failures (401 auth / 403 forbidden / 404
# not-found). These are not transient blips; a 60s cooldown makes the
# breaker re-probe a dead model every minute forever. Back off for a long
# window instead so the gateway leaves permanently-unavailable models alone.
PERMANENT_ERROR_COOLDOWN_SECS = float(
    os.environ.get("TUSKER_RETRY_PERMANENT_COOLDOWN", "3600")
)

# (provider, model) pairs observed returning a permanent 401/403. The
# auto-free pool skips these so genuinely-dead models (agentic-harness-only,
# WAF-blocked, wrong-tier) don't re-enter rotation.
PERMANENTLY_FAILED_MODELS: set[tuple[str, str]] = set()


def mark_permanently_failed(provider: str, model: str) -> None:
    """Record a (provider, model) that returned a permanent 401/403."""
    PERMANENTLY_FAILED_MODELS.add((provider, model))


def clear_permanently_failed(provider: str, model: str) -> None:
    """Clear a permanent-failure marker once the model recovers."""
    PERMANENTLY_FAILED_MODELS.discard((provider, model))


def is_permanently_failed(provider: str, model: str) -> bool:
    """Return True if this (provider, model) is known to be permanently dead."""
    return (provider, model) in PERMANENTLY_FAILED_MODELS



@dataclass
class Cooldown:
    until: float  # monotonic time when cooldown expires

    def is_active(self) -> bool:
        return time.monotonic() < self.until

    def remaining(self) -> float:
        r = self.until - time.monotonic()
        return max(0.0, r)


@dataclass
class CooldownTracker:
    _cooldowns: dict[tuple[str, str], Cooldown] = field(default_factory=dict)
    _provider_default: dict[str, Cooldown] = field(default_factory=dict)
    _group_cooldowns: dict[str, Cooldown] = field(default_factory=dict)
    _global: Cooldown | None = None
    _recent_failures: dict[str, int] = field(default_factory=dict)

    def record_failure(self, provider: str) -> bool:
        """Record failure. Returns True if provider cooldown triggered."""
        count = self._recent_failures.get(provider, 0) + 1
        self._recent_failures[provider] = count
        if count >= 3:
            self._recent_failures[provider] = 0
            return True
        return False

    def clear_failures(self, provider: str) -> None:
        self._recent_failures.pop(provider, None)

    def cooldown(
        self,
        provider: str,
        model: str,
        seconds: float,
    ) -> None:
        """Mark (provider, model) as in cooldown for `seconds`."""
        if seconds <= 0:
            return
        seconds = min(seconds, MAX_COOLDOWN_SECS)
        until = time.monotonic() + seconds
        self._cooldowns[(provider, model)] = Cooldown(until=until)
        # OpenRouter, Gemini, Groq, and Anthropic publish model/model-class
        # rate limits. Keep those cooldowns model-scoped; a 429 from one
        # model must not suppress healthy siblings. An empty model is the
        # explicit provider-wide sentinel used by the failure circuit.
        if not model or provider.lower() not in MODEL_SCOPED_COOLDOWN_PROVIDERS:
            # Keep the longest active provider-wide window. Multiple models
            # can report different Retry-After values during one outage; a
            # later, shorter response must not make the provider eligible
            # again before the longer window expires.
            current = self._provider_default.get(provider)
            if current is None or current.until < until:
                self._provider_default[provider] = Cooldown(until=until)
        logger.info('cooldown set %s/%s for %.0fs', provider, model, seconds)

    def cooldown_group(self, group: str, seconds: float) -> None:
        """Quarantine a shared upstream capacity group."""
        if not group or seconds <= 0:
            return
        seconds = min(seconds, MAX_COOLDOWN_SECS)
        until = time.monotonic() + seconds
        current = self._group_cooldowns.get(group)
        if current is None or current.until < until:
            self._group_cooldowns[group] = Cooldown(until=until)
        logger.info('capacity cooldown group=%s for %.0fs', group, seconds)

    def is_group_cooldown(self, group: str) -> bool:
        """Return True when a shared upstream capacity group is quarantined."""
        cooldown = self._group_cooldowns.get(group)
        return cooldown is not None and cooldown.is_active()

    def is_cooldown(self, provider: str, model: str) -> bool:
        """Return True if (provider, model) is in cooldown."""
        if self._global is not None and self._global.is_active():
            logger.debug('cooldown check %s/%s: active (global)', provider, model)
            return True
        # Import lazily to keep the low-level cooldown module independent of
        # the usage ledger during application import and test collection.
        try:
            from tusker_gateway.provider_usage import capacity_group_for_route

            group = capacity_group_for_route(provider, model)
        except Exception:
            group = None
        if group and self.is_group_cooldown(group):
            logger.debug(
                'cooldown check %s/%s: active (capacity group=%s)',
                provider,
                model,
                group,
            )
            return True
        key = (provider, model)
        if key in self._cooldowns and self._cooldowns[key].is_active():
            logger.debug('cooldown check %s/%s: active', provider, model)
            return True
        if provider in self._provider_default:
            co = self._provider_default[provider]
            if co.is_active():
                logger.debug('cooldown check %s/%s: active (provider default)', provider, model)
                return True
        logger.debug('cooldown check %s/%s: clear', provider, model)
        return False

    def clear(self, provider: str, model: str) -> None:
        """Clear cooldown for (provider, model)."""
        self._cooldowns.pop((provider, model), None)
        logger.debug('cooldown cleared %s/%s', provider, model)
        # Don't clear provider-default; keep it for batch eviction


def _cooldown_seconds_for_429(exc: dict[str, Any]) -> float:
    """Parse a 429 response body and return the cooldown seconds.

    Falls back to the Retry-After header value if present, otherwise
    uses a heuristic from the error body.

    Rules:
    - 401/403 on auth error → no retry
    - 429 with Retry-After → honor that value (cap at 3600)
    - 429 with "week" / "month" in body → 7 days
    - 429 with "hour" in body → 3600
    - 429 with "minute" in body → 3600 (default conservative)
    - 429 with numeric "N/day" → N*86400
    - Otherwise → 60 seconds
    """
    import aiohttp

    # If already an aiohttp.ClientResponseError
    if isinstance(exc, aiohttp.ClientResponseError):
        body = exc.message or ""
        headers = exc.headers or {}
    elif isinstance(exc, dict):
        body = str(exc.get("body", ""))
        headers = exc.get("headers", {})
    else:
        body = str(exc)
        headers = {}
    # Explicit Retry-After header
    ra = headers.get("Retry-After", "")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass

    # Parse body for limit windows
    body_lower = body.lower()

    if "retry-after" in body_lower:
        m = re.search(r"retry[- ]after[:\s]+(\d+)", body_lower)
        if m:
            return float(m.group(1))

    # Explicit "try again in N seconds/minutes/hours/days"
    m = re.search(
        r"(?:try again in|wait|after|cooldown|retry)[^.]*?(\d+)\s*(seconds?|minutes?|hours?|days?|weeks?)",
        body_lower,
    )
    if m:
        n, unit = float(m.group(1)), m.group(2)
        if unit.startswith("second"): return n
        if unit.startswith("minute"): return n * 60.0
        if unit.startswith("hour"): return n * 3600.0
        if unit.startswith("day"): return n * 86400.0
        if unit.startswith("week"): return n * 7 * 86400.0

    # Quota / usage-limit exhaustion is a long-lived state (the window won't
    # reset for hours or days), not a transient rate-limit blip. Backing off
    # for only 60s would hammer the upstream with pointless probes. Treat
    # these as a long cooldown so the gateway stops retrying until the quota
    # window plausibly resets.
    if any(hint in body_lower for hint in _QUOTA_HINTS):
        quota_cooldown = float(os.environ.get("TUSKER_RETRY_QUOTA_COOLDOWN", "3600"))
        logger.info(
            "429 cooldown: %.0fs for quota-exhausted provider", quota_cooldown
        )
        return quota_cooldown

    # Generic heuristic fallbacks
    if "week" in body_lower: return 7 * 86400.0
    if "month" in body_lower: return 30 * 86400.0
    if "hour" in body_lower: return 3600.0
    m = re.search(r"(\d+)\s*/\s*day", body_lower)
    if m: return 86400.0 / float(m.group(1))
    if "429" in body_lower or "rate limit" in body_lower:
        logger.info('429 cooldown: 60.0s for %s', 'unknown')
        return 60.0

    logger.info('429 cooldown: 60.0s for %s', 'unknown')
    return 60.0

def _cooldown_seconds_for_provider_error(exc: Any) -> float | None:
    """Derive a circuit-breaker cooldown for a non-429 provider error.

    Returns the number of seconds to back off, or ``None`` to fall back to
    the breaker policy cooldown. The policy default is 60s, which is correct
    only for transient blips; a permanently-dead model (auth failure, WAF
    block, agentic-harness-only, or a quota-gated not-found) would otherwise
    be re-probed every 60s forever.

    - 401 / 403 / 404 (and a quota/usage-limit body) → long cooldown.
    - 5xx (transient overload) → ``None`` (let the policy cooldown apply).
    """
    status = getattr(exc, "upstream_status", None)
    # Unknown status or a transient 5xx may recover; use the policy cooldown.
    if status is None or status >= 500:
        return None
    body = getattr(exc, "upstream_body", None) or ""
    body_lower = body.lower()
    # A quota-exhausted body on a non-429 status is a long-lived daily/monthly
    # window (e.g. OpenRouter "free-models-per-day-high-balance" surfaced as
    # 404). Back off until it plausibly resets, not 60s.
    if any(hint in body_lower for hint in _QUOTA_HINTS):
        return float(os.environ.get("TUSKER_RETRY_QUOTA_COOLDOWN", "3600"))
    # 401 auth / 403 forbidden / 404 not-found: permanent for this key/account.
    return PERMANENT_ERROR_COOLDOWN_SECS


# Module-level singleton shared across all request handlers
_tracker = CooldownTracker()


def global_tracker() -> CooldownTracker:
    return _tracker
