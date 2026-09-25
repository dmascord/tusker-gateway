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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import logging

from tusker_gateway.storage import StorageUnavailableError, shared_database

logger = logging.getLogger(__name__)


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
    path: str = field(default_factory=_default_path)
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
        self._db = None
        if not config.enabled:
            return
        try:
            self._db = shared_database(config.path, fallback_policy="critical")
            if not self._db.is_postgres:
                Path(config.path).parent.mkdir(parents=True, exist_ok=True)
            self._ensure_db()
        except StorageUnavailableError as exc:
            # Keep the limiter enabled and fail closed at request time.  A
            # PostgreSQL outage must not silently turn a shared limiter into
            # an uncoordinated SQLite/RWX limiter.
            logger.warning("rate limit state unavailable: %s", exc)
        except (PermissionError, OSError) as exc:
            logger.warning("rate limit disabled: cannot create %s: %s", config.path, exc)
            self._config.enabled = False

    def _ensure_db(self) -> None:
        assert self._db is not None
        with self._db.connection() as conn:
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
        assert self._db is not None
        with self._db.connection() as conn:
            # Refill and consume inside a single transaction so concurrent
            # checks cannot both read a sufficient balance and both consume.
            # The refill is expressed in SQL (CASE instead of MIN/MAX) so it
            # is atomic on both SQLite and PostgreSQL, and the consume is a
            # relative conditional decrement whose rowcount decides the winner.
            conn.execute(
                """UPDATE buckets SET
                    tokens = CASE
                        WHEN last_refill_at >= ? THEN tokens
                        ELSE CASE
                            WHEN tokens + (? - last_refill_at) * ? > ? THEN ?
                            ELSE tokens + (? - last_refill_at) * ?
                        END
                    END,
                    last_refill_at = ?
                WHERE fingerprint = ?""",
                (
                    now,           # last_refill_at >= now
                    now,           # (? - last_refill_at) now
                    policy.rate_per_sec,  # * rate_per_sec
                    policy.burst,  # > burst
                    policy.burst,  # THEN burst
                    now,           # second (? - last_refill_at) now
                    policy.rate_per_sec,  # * rate_per_sec
                    now,           # top-level last_refill_at = now
                    fp,            # WHERE fingerprint
                ),
            )
            updated = conn.execute(
                """
                UPDATE buckets SET tokens = tokens - ?
                WHERE fingerprint = ? AND tokens >= ?
                """,
                (cost, fp, cost),
            ).rowcount
            if not updated:
                # The bucket may not exist yet (first-insert race). Create it
                # with a full burst; ON CONFLICT DO NOTHING lets exactly one
                # writer win, then retry the conditional consume once.
                conn.execute(
                    """
                    INSERT INTO buckets (fingerprint, tokens, last_refill_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(fingerprint) DO NOTHING
                    """,
                    (fp, policy.burst, now),
                )
                updated = conn.execute(
                    """
                    UPDATE buckets SET tokens = tokens - ?
                    WHERE fingerprint = ? AND tokens >= ?
                    """,
                    (cost, fp, cost),
                ).rowcount
            row = conn.execute(
                "SELECT tokens FROM buckets WHERE fingerprint = ?",
                (fp,),
            ).fetchone()
            tokens = row[0] if row is not None else 0.0

        if updated:
            self.stats.allowed += 1
            logger.debug('rate limit check key=%s allowed=True', fp[:8])
            return RateLimitDecision(allowed=True, remaining=tokens)

        deficit = cost - tokens
        retry = deficit / policy.rate_per_sec if policy.rate_per_sec > 0 else 60.0
        self.stats.blocked += 1
        logger.warning('rate limit blocked key=%s (remaining=%.1f)', fp[:8], tokens)
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
        assert self._db is not None
        with self._db.connection() as conn:
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
        path=env.get("TUSKER_RATELIMIT_PATH") or _default_path(),
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
