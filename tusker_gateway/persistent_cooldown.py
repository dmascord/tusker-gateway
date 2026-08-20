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

from tusker_gateway.cooldown import Cooldown, CooldownTracker, MAX_COOLDOWN_SECS


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
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path), timeout=5)

    def record(self, provider: str, model: str, seconds: float) -> None:
        """Persist a (provider, model) cooldown until `seconds` from now."""
        if seconds <= 0:
            return
        seconds = min(seconds, MAX_COOLDOWN_SECS)
        until_epoch = time.time() + seconds
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cooldowns (provider, model, until_epoch, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    until_epoch = excluded.until_epoch,
                    updated_at = excluded.updated_at
                """,
                (provider, model, until_epoch, time.time()),
            )
            conn.commit()

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
            conn.commit()
        return cur.rowcount

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
        }