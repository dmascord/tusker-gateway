"""Provider cooldown tracking.

Tracks per-(provider, model) cooldown windows so requests are not
sent to a provider that is in a retry-throttling state.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any


# Maximum cooldown window is 1 hour.
MAX_COOLDOWN_SECS = 3600.0


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
    """Tracks cooldowns for (provider, model) pairs."""

    # Maps (provider, model) → Cooldown
    _cooldowns: dict[tuple[str, str], Cooldown] = field(default_factory=dict)
    _provider_default: dict[str, Cooldown] = field(default_factory=dict)
    _global: Cooldown | None = None

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

    def is_cooldown(self, provider: str, model: str) -> bool:
        """Return True if (provider, model) is in cooldown."""
        if self._global is not None and self._global.is_active():
            return True
        key = (provider, model)
        if key in self._cooldowns and self._cooldowns[key].is_active():
            return True
        if provider in self._provider_default:
            co = self._provider_default[provider]
            if co.is_active():
                return True
        return False

    def clear(self, provider: str, model: str) -> None:
        """Clear cooldown for (provider, model)."""
        self._cooldowns.pop((provider, model), None)
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
            return min(float(ra), MAX_COOLDOWN_SECS)
        except ValueError:
            pass

    # Parse body for limit windows
    body_lower = body.lower()

    if "retry-after" in body_lower:
        m = re.search(r"retry[- ]after[:\s]+(\d+)", body_lower)
        if m:
            return min(float(m.group(1)), MAX_COOLDOWN_SECS)

    # Weekly limit
    if "week" in body_lower:
        return min(7 * 86400.0, MAX_COOLDOWN_SECS)
    # Monthly limit
    if "month" in body_lower:
        return min(30 * 86400.0, MAX_COOLDOWN_SECS)
    # Hourly limit
    if "hour" in body_lower:
        return min(3600.0, MAX_COOLDOWN_SECS)
    # Daily limit
    m = re.search(r"(\d+)\s*/\s*day", body_lower)
    if m:
        return min(float(m.group(1)) * 86400.0, MAX_COOLDOWN_SECS)
    # Generic 429
    if "429" in body_lower or "rate limit" in body_lower:
        return 60.0

    return 60.0


# Module-level singleton shared across all request handlers
_tracker = CooldownTracker()


def global_tracker() -> CooldownTracker:
    return _tracker
