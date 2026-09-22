"""Short-lived durable storage for native tool approvals.

Approval records contain the exact tool call that must be replayed after the
client answers.  In production the shared state target is PostgreSQL, which
allows a retry to land on another gateway pod or after a rollout.  The store
is deliberately disabled when no shared PostgreSQL target is configured so
local development keeps the existing process-local behaviour.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tusker_gateway.storage import shared_database, state_database_url


_TABLE = "tusker_native_approvals"


class ApprovalStore:
    """Persist pending approval payloads with a bounded lifetime."""

    def __init__(self, path: str | Path):
        self._db = shared_database(path, timeout=5.0, fallback_policy="advisory")
        with self._db.connection() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    approval_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def load_active(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = time.time() if now is None else now
        records: dict[str, dict[str, Any]] = {}
        with self._db.connection() as conn:
            conn.execute(f"DELETE FROM {_TABLE} WHERE expires_at <= ?", (now,))
            for approval_id, payload, expires_at in conn.execute(
                f"SELECT approval_id, payload, expires_at FROM {_TABLE} WHERE expires_at > ?",
                (now,),
            ):
                try:
                    value = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    value["expires_at"] = float(expires_at)
                    records[str(approval_id)] = value
            conn.commit()
        return records

    def put(self, approval_id: str, pending: dict[str, Any]) -> None:
        payload = {key: value for key, value in pending.items() if key != "audit"}
        expires_at = float(pending.get("expires_at", time.time()))
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        with self._db.connection() as conn:
            conn.execute(
                f"""
                INSERT INTO {_TABLE} (approval_id, payload, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    payload = excluded.payload,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (approval_id, encoded, expires_at, now),
            )
            conn.commit()

    def delete(self, approval_id: str) -> None:
        with self._db.connection() as conn:
            conn.execute(f"DELETE FROM {_TABLE} WHERE approval_id = ?", (approval_id,))
            conn.commit()

    def clear(self) -> None:
        with self._db.connection() as conn:
            conn.execute(f"DELETE FROM {_TABLE}")
            conn.commit()


def configured_approval_store() -> ApprovalStore | None:
    """Build the shared approval store only when PostgreSQL is configured."""
    if not state_database_url():
        return None
    import os

    path = os.environ.get("TUSKER_APPROVAL_DB_PATH", "").strip()
    if not path:
        path = str(Path(os.environ.get("HOME", ".")) / ".hermes" / "approvals.db")
    return ApprovalStore(path)

