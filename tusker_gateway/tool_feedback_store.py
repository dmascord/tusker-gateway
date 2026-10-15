"""Shared, content-free tool-call correlation state.

Uses the gateway shared PostgreSQL backend when configured; otherwise SQLite.
Call IDs are represented by SHA-256 digests, never raw identifiers.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from tusker_gateway.storage import shared_database, storage_error_types

log = logging.getLogger(__name__)
_TTL = 3600
_MAX_ROWS = 8192


def _db():
    path = os.environ.get("TUSKER_TOOL_FEEDBACK_PATH", "").strip()
    if not path:
        root = Path("/home/tusker/.hermes")
        path = str(root / "tool-feedback.db") if root.exists() else "/tmp/tusker-tool-feedback.db"
    return shared_database(path, fallback_policy="advisory")


def _key(call_id: str) -> str:
    # OpenAI call IDs are opaque and high entropy; deterministic hashing
    # permits cross-replica correlation without sharing process-local secrets.
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()


def _prepare(db: Any) -> None:
    with db.connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tool_feedback_pending ("
            "id_hash VARCHAR(64) PRIMARY KEY, created_at BIGINT NOT NULL, "
            "request_id VARCHAR(128) NOT NULL)"
        )


def register(call_ids: list[str], request_id: str | None = None) -> int:
    ids = [_key(x) for x in call_ids[:128] if isinstance(x, str) and x]
    if not ids:
        return 0
    try:
        db = _db()
        _prepare(db)
        now = int(time.time())
        with db.connection() as conn:
            conn.execute("DELETE FROM tool_feedback_pending WHERE created_at < ?", (now - _TTL,))
            for digest in ids:
                conn.execute(
                    "INSERT INTO tool_feedback_pending (id_hash, created_at, request_id) "
                    "VALUES (?, ?, ?) ON CONFLICT (id_hash) DO UPDATE SET "
                    "created_at=excluded.created_at, request_id=excluded.request_id",
                    (digest, now, (request_id or "")[:128]),
                )
            conn.execute(
                "DELETE FROM tool_feedback_pending WHERE id_hash IN "
                "(SELECT id_hash FROM tool_feedback_pending ORDER BY created_at DESC "
                "LIMIT -1 OFFSET ?)" if not conn.is_postgres else
                "DELETE FROM tool_feedback_pending WHERE id_hash IN "
                "(SELECT id_hash FROM tool_feedback_pending ORDER BY created_at DESC "
                "OFFSET ?)",
                (_MAX_ROWS,),
            )
        return len(ids)
    except storage_error_types() + (RuntimeError,):
        log.warning("tool feedback shared state unavailable", exc_info=False)
        return 0


def consume(call_id: str) -> bool:
    try:
        db = _db()
        _prepare(db)
        with db.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM tool_feedback_pending WHERE id_hash=? AND created_at >= ?",
                (_key(call_id), int(time.time()) - _TTL),
            )
            return bool(cursor.rowcount and cursor.rowcount > 0)
    except storage_error_types() + (RuntimeError,):
        log.warning("tool feedback shared state unavailable", exc_info=False)
        return False
