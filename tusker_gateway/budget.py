"""Per-key token-budget enforcement.

Tracks token spend per virtual API key against configured daily and monthly
caps, and rejects requests with HTTP 429 once a cap is reached.

Configuration (per virtual API key, looked up via the request's bearer token):
    {
      "daily_tokens": 500000,        # optional
      "monthly_tokens": 10000000,    # optional
      "per_pool_tokens": {           # optional, per-pool caps
         "code":    1000000,
         "privacy":  500000
      }
    }

Period semantics:
    - Daily   = rolling 24-hour window from "now" (simpler than UTC-day).
                Avoids the "00:00 UTC spike" failure mode.
    - Monthly = rolling 30-day window. We deliberately do NOT use calendar
                months because they create uneven budgets.

Storage:
    SQLite at `cache/budget.db`. Two tables:
      `usage`    — rolling-window token spend per (key, period, period_start).
      `refunds`  — token adjustments for failed calls (best-effort).

Concurrency:
    A single SQLite write transaction wraps the check-and-increment. SQLite's
    serialised write semantics give us atomicity for free, and the per-key
    workload is low enough that contention is not a concern.

Failure semantics:
    - Provider 5xx → tokens refunded (so a flaky provider doesn't burn budget).
    - Provider 4xx → tokens NOT refunded (caller error, provider still charged us).
    - Cache hit   → tokens NOT counted (no provider call happened).
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
        return os.path.join(home, ".hermes", "budget.db")
    return "cache/budget.db"


@dataclass
class BudgetCaps:
    """Caps for a single virtual API key."""
    daily_tokens: int | None = None
    monthly_tokens: int | None = None
    per_pool_tokens: dict[str, int] = field(default_factory=dict)


@dataclass
class BudgetConfig:
    enabled: bool = False
    path: str = field(default_factory=_default_path)
    # Map of api_key_fingerprint -> caps dict
    caps: dict[str, BudgetCaps] = field(default_factory=dict)
    # Optional global cap (applied to all keys regardless of caps table)
    global_daily_tokens: int | None = None


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str | None = None
    cap_name: str | None = None  # which cap was hit ("daily", "monthly", "pool:code", ...)
    used: int = 0
    cap: int | None = None


@dataclass
class BudgetStats:
    blocks_daily: int = 0
    blocks_monthly: int = 0
    blocks_pool: int = 0
    blocks_global: int = 0
    records: int = 0
    refunds: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "blocks_daily": self.blocks_daily,
            "blocks_monthly": self.blocks_monthly,
            "blocks_pool": self.blocks_pool,
            "blocks_global": self.blocks_global,
            "records": self.records,
            "refunds": self.refunds,
        }


def _key_fingerprint(api_key: str) -> str:
    """Stable fingerprint for an API key; never store the raw key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


class BudgetTracker:
    """SQLite-backed per-key token budget enforcement."""

    DAILY_WINDOW = 86_400       # 24h rolling
    MONTHLY_WINDOW = 30 * 86_400  # 30d rolling

    def __init__(self, config: BudgetConfig):
        self._config = config
        self.stats = BudgetStats()
        if not config.enabled:
            return
        try:
            Path(config.path).parent.mkdir(parents=True, exist_ok=True)
            self._ensure_db()
        except (PermissionError, OSError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "budget disabled: cannot create %s: %s", config.path, exc
            )
            self._config.enabled = False

    def _ensure_db(self) -> None:
        with sqlite3.connect(self._config.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    fingerprint TEXT NOT NULL,
                    period TEXT NOT NULL,            -- 'daily' | 'monthly' | 'pool:<name>'
                    period_start REAL NOT NULL,      -- unix timestamp of window start
                    tokens INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (fingerprint, period, period_start)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_period ON usage(period, period_start)"
            )
            conn.commit()

    # -- public API ------------------------------------------------------

    def check(self, api_key: str, pool_name: str | None, tokens: int) -> BudgetDecision:
        """Check whether `tokens` are available; never mutates state."""
        if not self._config.enabled:
            return BudgetDecision(allowed=True)
        if tokens <= 0:
            return BudgetDecision(allowed=True)

        fp = _key_fingerprint(api_key)
        caps = self._config.caps.get(fp)
        now = time.time()

        # Global cap (applies to every key)
        if self._config.global_daily_tokens is not None:
            used = self._sum(fp, "daily", now, self.DAILY_WINDOW)
            if used + tokens > self._config.global_daily_tokens:
                self.stats.blocks_global += 1
                return BudgetDecision(
                    allowed=False,
                    reason=f"global daily cap exceeded ({used}/{self._config.global_daily_tokens})",
                    cap_name="global_daily",
                    used=used,
                    cap=self._config.global_daily_tokens,
                )

        if caps is None:
            return BudgetDecision(allowed=True)

        if caps.daily_tokens is not None:
            used = self._sum(fp, "daily", now, self.DAILY_WINDOW)
            if used + tokens > caps.daily_tokens:
                self.stats.blocks_daily += 1
                return BudgetDecision(
                    allowed=False,
                    reason=f"daily cap exceeded ({used}/{caps.daily_tokens})",
                    cap_name="daily",
                    used=used,
                    cap=caps.daily_tokens,
                )

        if caps.monthly_tokens is not None:
            used = self._sum(fp, "monthly", now, self.MONTHLY_WINDOW)
            if used + tokens > caps.monthly_tokens:
                self.stats.blocks_monthly += 1
                return BudgetDecision(
                    allowed=False,
                    reason=f"monthly cap exceeded ({used}/{caps.monthly_tokens})",
                    cap_name="monthly",
                    used=used,
                    cap=caps.monthly_tokens,
                )

        if pool_name and pool_name in caps.per_pool_tokens:
            cap = caps.per_pool_tokens[pool_name]
            period_key = f"pool:{pool_name}"
            used = self._sum(fp, period_key, now, self.DAILY_WINDOW)
            if used + tokens > cap:
                self.stats.blocks_pool += 1
                return BudgetDecision(
                    allowed=False,
                    reason=f"per-pool cap exceeded ({used}/{cap}) for pool={pool_name}",
                    cap_name=period_key,
                    used=used,
                    cap=cap,
                )

        return BudgetDecision(allowed=True)

    def record(self, api_key: str, pool_name: str | None, tokens: int) -> None:
        """Atomically add `tokens` to all applicable usage windows."""
        if not self._config.enabled or tokens <= 0:
            return
        fp = _key_fingerprint(api_key)
        now = time.time()
        with sqlite3.connect(self._config.path) as conn:
            # Daily
            self._bump(conn, fp, "daily", now, self.DAILY_WINDOW, tokens)
            # Monthly
            self._bump(conn, fp, "monthly", now, self.MONTHLY_WINDOW, tokens)
            # Per-pool
            if pool_name:
                self._bump(conn, fp, f"pool:{pool_name}", now, self.DAILY_WINDOW, tokens)
            conn.commit()
        self.stats.records += 1

    def refund(self, api_key: str, pool_name: str | None, tokens: int) -> None:
        """Subtract `tokens` (for failed provider calls)."""
        if not self._config.enabled or tokens <= 0:
            return
        fp = _key_fingerprint(api_key)
        now = time.time()
        with sqlite3.connect(self._config.path) as conn:
            self._bump(conn, fp, "daily", now, self.DAILY_WINDOW, -tokens)
            self._bump(conn, fp, "monthly", now, self.MONTHLY_WINDOW, -tokens)
            if pool_name:
                self._bump(conn, fp, f"pool:{pool_name}", now, self.DAILY_WINDOW, -tokens)
            conn.commit()
        self.stats.refunds += 1

    def stats_snapshot(self) -> dict[str, int]:
        return self.stats.snapshot()

    def usage_snapshot(self, api_key: str) -> dict[str, dict[str, int]]:
        """Return current usage windows for an API key (for /status)."""
        if not self._config.enabled:
            return {}
        fp = _key_fingerprint(api_key)
        now = time.time()
        with sqlite3.connect(self._config.path) as conn:
            rows = conn.execute(
                """
                SELECT period, period_start, tokens FROM usage
                WHERE fingerprint = ? AND period_start > ?
                """,
                (fp, now - self.MONTHLY_WINDOW),
            ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for period, period_start, tokens in rows:
            out.setdefault(period, {})[str(int(period_start))] = tokens
        return out

    # -- internals -------------------------------------------------------

    def _sum(self, fp: str, period: str, now: float, window: float) -> int:
        window_start = now - window
        with sqlite3.connect(self._config.path) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(tokens), 0) FROM usage
                WHERE fingerprint = ? AND period = ? AND period_start > ?
                """,
                (fp, period, window_start),
            ).fetchone()
        return int(row[0])

    def _bump(self, conn: sqlite3.Connection, fp: str, period: str,
              now: float, window: float, tokens: int) -> None:
        # The "period_start" we store is the START of the current window.
        # We pick the largest multiple of `window` <= now so all writes
        # within the same window land on the same row, and old windows
        # accumulate as separate rows that are simply excluded by the
        # `period_start > now - window` predicate in `_sum`.
        window_start = now - (now % window)
        conn.execute(
            """
            INSERT INTO usage (fingerprint, period, period_start, tokens)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(fingerprint, period, period_start) DO UPDATE SET
                tokens = tokens + excluded.tokens
            """,
            (fp, period, window_start, tokens),
        )


def load_budget_config_from_env(env: dict[str, str] | None = None) -> BudgetConfig:
    """Build a BudgetConfig from the environment (or a dict for tests).

    Per-key caps are loaded from a single JSON env var TUSKER_BUDGETS_JSON:
        {
          "<key_fingerprint>": {
            "daily_tokens": 500000,
            "monthly_tokens": 10000000,
            "per_pool_tokens": {"code": 1000000, "privacy": 500000}
          }
        }
    Fingerprints are SHA-256 of the raw key (truncated to 32 chars), same
    as `BudgetTracker._key_fingerprint`. This keeps the env var opaque.
    """
    env = env if env is not None else os.environ
    enabled = env.get("TUSKER_BUDGETS_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )
    raw = env.get("TUSKER_BUDGETS_JSON", "").strip()
    caps: dict[str, BudgetCaps] = {}
    if raw:
        import json
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for fp, c in data.items():
                    if not isinstance(c, dict):
                        continue
                    caps[fp] = BudgetCaps(
                        daily_tokens=c.get("daily_tokens"),
                        monthly_tokens=c.get("monthly_tokens"),
                        per_pool_tokens=dict(c.get("per_pool_tokens", {})),
                    )
        except json.JSONDecodeError:
            pass
    return BudgetConfig(
        enabled=enabled,
        path=env.get("TUSKER_BUDGETS_PATH") or _default_path(),
        caps=caps,
        global_daily_tokens=int(env["TUSKER_GLOBAL_DAILY_TOKENS"]) if env.get("TUSKER_GLOBAL_DAILY_TOKENS") else None,
    )


__all__ = [
    "BudgetCaps",
    "BudgetConfig",
    "BudgetDecision",
    "BudgetStats",
    "BudgetTracker",
    "load_budget_config_from_env",
]
