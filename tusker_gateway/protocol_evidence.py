"""Versioned evidence for upstream protocol behaviour.

Protocol probes are intentionally distinct from modality/capability probes.
This module only records results supplied by a real probe runner; it never
claims that a model supports a dialect based on its name.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from tusker_gateway.storage import shared_database

PROTOCOL_PROBE_VERSION = "protocol-contract-v1"
PROTOCOL_FEATURES = frozenset({
    "structured_tool_calls", "tool_call_finish", "reasoning_channel",
    "reasoning_replay", "reasoning_effort", "thinking_parameter",
    "streaming_tool_fragments", "responses_function_calls",
})
PROTOCOL_STATUSES = frozenset({"passed", "unsupported", "unavailable", "unknown"})
MAX_EVIDENCE_AGE_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class ProtocolEvidence:
    provider: str
    endpoint_id: str
    model: str
    feature: str
    status: str
    probe_version: str
    checked_at: float

    @property
    def verified(self) -> bool:
        return self.status == "passed" and self.probe_version == PROTOCOL_PROBE_VERSION and (
            time.time() - self.checked_at
        ) <= MAX_EVIDENCE_AGE_SECONDS


class ProtocolEvidenceStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("TUSKER_PROTOCOL_EVIDENCE_DB_PATH") or str(
            Path(os.getenv("TUSKER_QUALITY_DB_PATH", "/tmp/tusker-quality.db")).with_name("protocol-evidence.db")
        )
        self.db = shared_database(self.path)
        if not self.db.is_postgres and self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.db.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS protocol_evidence ("
                "provider TEXT NOT NULL, endpoint_id TEXT NOT NULL, model TEXT NOT NULL,"
                "feature TEXT NOT NULL, status TEXT NOT NULL, probe_version TEXT NOT NULL,"
                "checked_at REAL NOT NULL,"
                "PRIMARY KEY (provider, endpoint_id, model, feature))"
            )
            conn.commit()

    def record(self, provider: str, endpoint_id: str, model: str, feature: str,
               status: str, *, checked_at: float | None = None) -> None:
        if feature not in PROTOCOL_FEATURES or status not in PROTOCOL_STATUSES:
            raise ValueError("unsupported protocol feature or evidence status")
        if not provider or not endpoint_id or not model:
            raise ValueError("provider, endpoint_id and model are required")
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO protocol_evidence "
                "(provider, endpoint_id, model, feature, status, probe_version, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (provider, endpoint_id, model, feature) DO UPDATE SET "
                "status=excluded.status, probe_version=excluded.probe_version,"
                "checked_at=excluded.checked_at",
                (provider.lower(), endpoint_id, model, feature, status,
                 PROTOCOL_PROBE_VERSION, time.time() if checked_at is None else checked_at),
            )
            conn.commit()

    def get(self, provider: str, endpoint_id: str, model: str,
            feature: str) -> ProtocolEvidence | None:
        if feature not in PROTOCOL_FEATURES:
            raise ValueError("unsupported protocol feature")
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT provider, endpoint_id, model, feature, status, probe_version, checked_at "
                "FROM protocol_evidence WHERE provider=? AND endpoint_id=? AND model=? AND feature=?",
                (provider.lower(), endpoint_id, model, feature),
            ).fetchone()
        return ProtocolEvidence(*row) if row else None
