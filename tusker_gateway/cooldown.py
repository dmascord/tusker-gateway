"""Provider cooldown tracking.

Tracks per-(provider, model) cooldown windows so requests are not
sent to a provider that is in a retry-throttling state.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import logging

logger = logging.getLogger(__name__)


MAX_COOLDOWN_SECS = 30 * 86400.0 # 30 days



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
        # Also track at provider level
        self._provider_default[provider] = Cooldown(until=until)
        logger.info('cooldown set %s/%s for %.0fs', provider, model, seconds)
    def is_cooldown(self, provider: str, model: str) -> bool:
        """Return True if (provider, model) is in cooldown."""
        if self._global is not None and self._global.is_active():
            logger.debug('cooldown check %s/%s: active (global)', provider, model)
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


# Module-level singleton shared across all request handlers
_tracker = CooldownTracker()


def global_tracker() -> CooldownTracker:
    return _tracker
