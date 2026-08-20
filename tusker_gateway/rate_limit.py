"""Per-API-key rate limiting via token bucket.

A token bucket is the standard algorithm for smooth rate limiting: the bucket
holds up to `burst` tokens, refilled at `rate_per_sec` tokens per second. Each
request consumes `cost` tokens (default 1). When the bucket is empty, requests
are rejected with HTTP 429.

Configuration per virtual API key (looked up by SHA-256 fingerprint, same as
BudgetTracker):
    {
      "rate_per_sec": 10,    # refill rate
      "burst": 50,           # max bucket size
      "cost_per_request": 1  # tokens consumed per call (default 1)
    }

Persistence:
    SQLite at the configured path. We persist `tokens` and `last_refill_at`
    so a restart doesn't reset every key to a full bucket. The refill math
    runs in `_refill()` which is called inside `check()`.

Pre-flight vs post-flight:
    We do pre-flight — return 429 BEFORE calling the provider. This is the
    common case but has one edge: a request that we accept may itself fail
    (network error, etc.). The token is NOT refunded in that case because
    we already consumed the upstream capacity slot.

Headers (when limited):
    Retry-After: <seconds until next token>
    X-Tusker-RateLimit-Remaining: <tokens>
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _default_path() -> str:
    home = os.environ.get("HOME", "")
    if home:
        return os.path.join(home, ".hermes", "ratelimit.db")
    return "cache/ratelimit.db"


@dataclass
class RateLimitPolicy:
    rate_per_sec: float = 10.0
    burst: float = 50.0
    cost_per_request: float = 1.0


@dataclass
class RateLimitConfig:
    enabled: bool = False
    path: str = _default_path()
    policies: dict[str, RateLimitPolicy] = field(default_factory=dict)
    # Default policy applied to keys without explicit entries.
    default_policy: RateLimitPolicy | None = None


@dataclass
class RateLimitDecision:
    allowed: bool
    remaining: float = 0.0
    retry_after: float = 0.0
    reason: str | None = None


@dataclass
class RateLimitStats:
    checks: int = 0
    allowed: int = 0
    blocked: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "checks": self.checks,
            "allowed": self.allowed,
            "blocked": self.blocked,
        }


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


class RateLimiter:
    """SQLite-backed per-API-key token-bucket rate limiter."""

    def __init__(self, config: RateLimitConfig):
        self._config = config
        self.stats = RateLimitStats()
        if not config.enabled:
            return
        try:
            Path(config.path).parent.mkdir(parents=True, exist_ok=True)
            self._ensure_db()
        except (PermissionError, OSError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "rate limit disabled: cannot create %s: %s", config.path, exc
            )
            self._config.enabled = False

    def _ensure_db(self) -> None:
        with sqlite3.connect(self._config.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS buckets (
                    fingerprint TEXT PRIMARY KEY,
                    tokens REAL NOT NULL,
                    last_refill_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _policy_for(self, api_key: str) -> RateLimitPolicy | None:
        if not self._config.enabled:
            return None
        fp = _key_fingerprint(api_key)
        if fp in self._config.policies:
            return self._config.policies[fp]
        return self._config.default_policy

    def check(self, api_key: str, cost: float | None = None) -> RateLimitDecision:
        """Consume tokens for this key. Refuses if insufficient tokens."""
        if not self._config.enabled:
            return RateLimitDecision(allowed=True)
        policy = self._policy_for(api_key)
        if policy is None:
            return RateLimitDecision(allowed=True)
        if not api_key:
            return RateLimitDecision(allowed=True)

        cost = cost if cost is not None else policy.cost_per_request
        fp = _key_fingerprint(api_key)
        now = time.time()

        self.stats.checks += 1
        with sqlite3.connect(self._config.path) as conn:
            row = conn.execute(
                "SELECT tokens, last_refill_at FROM buckets WHERE fingerprint = ?",
                (fp,),
            ).fetchone()

        if row is None:
            # First time we see this key — start with a full bucket.
            tokens = policy.burst
            last_refill = now
        else:
            tokens, last_refill = row
            # Refill: tokens += rate * elapsed_secs, capped at burst.
            elapsed = max(0.0, now - last_refill)
            tokens = min(policy.burst, tokens + elapsed * policy.rate_per_sec)

        if tokens >= cost:
            tokens -= cost
            with sqlite3.connect(self._config.path) as conn:
                conn.execute(
                    """
                    INSERT INTO buckets (fingerprint, tokens, last_refill_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        tokens = excluded.tokens,
                        last_refill_at = excluded.last_refill_at
                    """,
                    (fp, tokens, now),
                )
                conn.commit()
            self.stats.allowed += 1
            return RateLimitDecision(allowed=True, remaining=tokens)
        else:
            # Persist the refilled amount so we don't lose refill progress.
            with sqlite3.connect(self._config.path) as conn:
                conn.execute(
                    """
                    INSERT INTO buckets (fingerprint, tokens, last_refill_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        tokens = excluded.tokens,
                        last_refill_at = excluded.last_refill_at
                    """,
                    (fp, tokens, now),
                )
                conn.commit()
            deficit = cost - tokens
            retry = deficit / policy.rate_per_sec if policy.rate_per_sec > 0 else 60.0
            self.stats.blocked += 1
            return RateLimitDecision(
                allowed=False,
                remaining=tokens,
                retry_after=retry,
                reason=f"rate limit exceeded (refill {policy.rate_per_sec}/s, burst {policy.burst})",
            )

    def stats_snapshot(self) -> dict[str, int]:
        return self.stats.snapshot()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-key bucket state for the dashboard."""
        if not self._config.enabled:
            return {}
        with sqlite3.connect(self._config.path) as conn:
            rows = conn.execute(
                "SELECT fingerprint, tokens, last_refill_at FROM buckets"
            ).fetchall()
        return {
            fp: {"tokens": t, "last_refill_at": ts}
            for fp, t, ts in rows
        }


def load_rate_limit_config_from_env(env: dict[str, str] | None = None) -> RateLimitConfig:
    env = env if env is not None else os.environ
    enabled = env.get("TUSKER_RATELIMIT_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )
    policies: dict[str, RateLimitPolicy] = {}
    default: RateLimitPolicy | None = None
    raw = env.get("TUSKER_RATELIMIT_JSON", "").strip()
    if raw:
        import json
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                # Two shapes supported:
                #   {"<fp>": {"rate_per_sec": 10, "burst": 50}}
                #   {"default": {...}, "<fp>": {...}}
                for k, v in data.items():
                    if not isinstance(v, dict):
                        continue
                    policy = RateLimitPolicy(
                        rate_per_sec=float(v.get("rate_per_sec", 10.0)),
                        burst=float(v.get("burst", 50.0)),
                        cost_per_request=float(v.get("cost_per_request", 1.0)),
                    )
                    if k == "default":
                        default = policy
                        continue
                    policies[k] = policy
        except json.JSONDecodeError:
            pass

    # Top-level default (overrides JSON "default" if both are set).
    if env.get("TUSKER_RATELIMIT_DEFAULT_RATE"):
        default = RateLimitPolicy(
            rate_per_sec=float(env["TUSKER_RATELIMIT_DEFAULT_RATE"]),
            burst=float(env.get("TUSKER_RATELIMIT_DEFAULT_BURST", "50")),
        )
    return RateLimitConfig(
        enabled=enabled,
        path=env.get("TUSKER_RATELIMIT_PATH", "cache/ratelimit.db"),
        policies=policies,
        default_policy=default,
    )


__all__ = [
    "RateLimitConfig",
    "RateLimitDecision",
    "RateLimitPolicy",
    "RateLimitStats",
    "RateLimiter",
    "load_rate_limit_config_from_env",
]
