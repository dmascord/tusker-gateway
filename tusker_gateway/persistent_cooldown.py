"""Persistent cooldown store.

Wraps `CooldownTracker` with SQLite-backed persistence so cooldowns survive
container restarts.  Records use wall-clock time, not monotonic, so we can
differentiate "still active" from "stale" after a restart.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import logging

logger = logging.getLogger(__name__)

from tusker_gateway.cooldown import (
    MODEL_SCOPED_COOLDOWN_PROVIDERS,
    Cooldown,
    CooldownTracker,
    MAX_COOLDOWN_SECS,
)


@dataclass
class PersistentCooldownStore:
    db_path: Path

    def __post_init__(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cooldowns (
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    until_epoch REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (provider, model)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_cooldowns (
                    provider TEXT PRIMARY KEY,
                    until_epoch REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS capacity_group_cooldowns (
                    group_name TEXT PRIMARY KEY,
                    until_epoch REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path), timeout=5)

    def record(self, provider: str, model: str, seconds: float) -> None:
        """Persist a (provider, model) cooldown until `seconds` from now."""
        if seconds <= 0:
            return
        seconds = min(seconds, MAX_COOLDOWN_SECS)
        now = time.time()
        until_epoch = now + seconds
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cooldowns (provider, model, until_epoch, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    until_epoch = excluded.until_epoch,
                    updated_at = excluded.updated_at
                """,
                (provider, model, until_epoch, now),
            )
            # Cooldowns are provider-wide unless the provider publishes
            # model-scoped limits. Persist the same scope that
            # CooldownTracker enforces in memory so a restart cannot
            # immediately probe every model in a quota-exhausted provider.
            if not model or provider.lower() not in MODEL_SCOPED_COOLDOWN_PROVIDERS:
                self._upsert_provider(conn, provider, until_epoch, now)
            conn.commit()
        logger.debug('persisted cooldown %s/%s for %.0fs', provider, model, seconds)

    @staticmethod
    def _upsert_provider(
        conn: sqlite3.Connection,
        provider: str,
        until_epoch: float,
        updated_at: float,
    ) -> None:
        """Store the longest active provider-wide cooldown."""
        conn.execute(
            """
            INSERT INTO provider_cooldowns (provider, until_epoch, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                until_epoch = MAX(provider_cooldowns.until_epoch, excluded.until_epoch),
                updated_at = excluded.updated_at
            """,
            (provider, until_epoch, updated_at),
        )

    def record_provider(self, provider: str, seconds: float) -> None:
        """Persist a provider cooldown until `seconds` from now."""
        if seconds <= 0:
            return
        seconds = min(seconds, MAX_COOLDOWN_SECS)
        now = time.time()
        until_epoch = now + seconds
        with self._connect() as conn:
            self._upsert_provider(conn, provider, until_epoch, now)
            conn.commit()

    def record_group(self, group: str, seconds: float) -> None:
        """Persist a shared provider-capacity quarantine window."""
        if not group or seconds <= 0:
            return
        seconds = min(seconds, MAX_COOLDOWN_SECS)
        now = time.time()
        until_epoch = now + seconds
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO capacity_group_cooldowns (group_name, until_epoch, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(group_name) DO UPDATE SET
                    until_epoch = MAX(capacity_group_cooldowns.until_epoch, excluded.until_epoch),
                    updated_at = excluded.updated_at
                """,
                (group, until_epoch, now),
            )
            conn.commit()

    def hydrate_groups(self, tracker: CooldownTracker) -> int:
        """Load active capacity-group quarantines into memory."""
        now_wall = time.time()
        loaded = 0
        with self._connect() as conn:
            for group, until_epoch, _updated in conn.execute(
                "SELECT group_name, until_epoch, updated_at FROM capacity_group_cooldowns"
            ):
                remaining = until_epoch - now_wall
                if remaining <= 0:
                    continue
                tracker.cooldown_group(group, remaining)
                loaded += 1
        logger.info('hydrated %d capacity group cooldowns from store', loaded)
        return loaded

    def is_provider_active(self, provider: str) -> bool:
        """Return True if provider cooldown is still active."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT until_epoch FROM provider_cooldowns WHERE provider = ?",
                (provider,),
            ).fetchone()
        if not row:
            return False
        return row[0] > time.time()

    def hydrate_providers(self, tracker: CooldownTracker) -> int:
        """Load active provider cooldowns into an in-memory `CooldownTracker`."""
        now_wall = time.time()
        loaded = 0
        with self._connect() as conn:
            for provider, until_epoch, _updated in conn.execute(
                "SELECT provider, until_epoch, updated_at FROM provider_cooldowns"
            ):
                remaining = until_epoch - now_wall
                if remaining <= 0:
                    continue
                tracker.cooldown(provider, "", remaining) # model='' sentinel
                loaded += 1
        logger.info('hydrated %d provider cooldowns from store', loaded)
        return loaded
    def is_active(self, provider: str, model: str) -> bool:
        """Return True if (provider, model) cooldown is still active in storage."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT until_epoch FROM cooldowns WHERE provider = ? AND model = ?",
                (provider, model),
            ).fetchone()
        if not row:
            return False
        return row[0] > time.time()

    def purge_expired(self) -> int:
        """Remove cooldown rows whose window has elapsed. Returns deleted count."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM cooldowns WHERE until_epoch <= ?", (time.time(),)
            )
            group_cur = conn.execute(
                "DELETE FROM capacity_group_cooldowns WHERE until_epoch <= ?",
                (time.time(),),
            )
            conn.commit()
        count = cur.rowcount + group_cur.rowcount
        logger.info('purged %d expired cooldowns', count)
        return count

    def hydrate(self, tracker: CooldownTracker) -> int:
        """Load active cooldowns into an in-memory `CooldownTracker`.

        Returns the number of entries hydrated.
        """
        now = time.monotonic()
        now_wall = time.time()
        loaded = 0
        with self._connect() as conn:
            for provider, model, until_epoch, _updated in conn.execute(
                "SELECT provider, model, until_epoch, updated_at FROM cooldowns"
            ):
                remaining = until_epoch - now_wall
                if remaining <= 0:
                    continue
                # Use monotonic remaining time in the in-memory tracker
                tracker.cooldown(provider, model, remaining)
                loaded += 1
        logger.info('hydrated %d cooldowns from store', loaded)
        return loaded

    def clear(self, provider: str, model: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM cooldowns WHERE provider = ? AND model = ?",
                (provider, model),
            )
            conn.commit()

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT provider, model, until_epoch FROM cooldowns ORDER BY until_epoch DESC"
            ).fetchall()
            group_rows = conn.execute(
                "SELECT group_name, until_epoch FROM capacity_group_cooldowns ORDER BY until_epoch DESC"
            ).fetchall()
        return {
            "active_count": len(rows),
            "entries": [
                {
                    "provider": p,
                    "model": m,
                    "until_epoch": u,
                    "seconds_remaining": max(0.0, u - time.time()),
                }
                for p, m, u in rows
            ],
            "capacity_groups": [
                {
                    "group": group,
                    "until_epoch": until_epoch,
                    "seconds_remaining": max(0.0, until_epoch - time.time()),
                }
                for group, until_epoch in group_rows
            ],
        }
