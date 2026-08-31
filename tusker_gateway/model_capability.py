"""Persistent model capability evidence.

Transport quality and tool-call qualification answer different questions from
modality support. This module stores the latest evidence for capabilities such
as ``input_image`` and ``image_generations`` without retaining request bodies,
response bodies, or credentials.

The status is deliberately evidence-oriented:

``advertised``
    The provider catalog claims the capability.
``discovered``
    The provider capability registry found a matching provider surface.
``passed``
    An explicit, bounded capability probe succeeded.
``unsupported``
    The provider/model explicitly rejected the capability.
``unavailable``
    Authentication, quota, capacity, transport, or timeout prevented a
    determination. This must be retried rather than treated as unsupported.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator


MODEL_CAPABILITY_PROBE_VERSION = "model-capability-v1"

INPUT_MODALITY_CAPABILITIES = frozenset({
    "input_text",
    "input_image",
    "input_audio",
    "input_video",
})
OUTPUT_MODALITY_CAPABILITIES = frozenset({
    "output_text",
    "output_image",
    "output_audio",
    "output_video",
})
ENDPOINT_CAPABILITIES = frozenset({
    "image_generations",
    "image_edits",
    "image_variations",
    "tts_speech",
    "video_generations",
})
KNOWN_CAPABILITIES = (
    INPUT_MODALITY_CAPABILITIES
    | OUTPUT_MODALITY_CAPABILITIES
    | ENDPOINT_CAPABILITIES
)
CAPABILITY_STATUSES = frozenset({
    "advertised",
    "discovered",
    "passed",
    "unsupported",
    "unavailable",
    "unknown",
})


def normalize_capability(value: str) -> str:
    """Normalize a capability identifier for stable database keys."""
    return str(value).strip().lower().replace("-", "_")


@dataclass(frozen=True)
class ModelCapability:
    """Latest evidence for one provider/model/capability tuple."""

    provider: str
    model: str
    capability: str
    status: str
    source: str
    probe_version: str
    http_status: int | None
    latency_ms: float | None
    failure_class: str | None
    checked_at: float

    @property
    def verified(self) -> bool:
        """Whether an explicit live probe passed."""
        return self.status == "passed"

    @property
    def transient(self) -> bool:
        """Whether the result is an availability failure, not a capability failure."""
        return self.status == "unavailable"

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["verified"] = self.verified
        data["transient"] = self.transient
        return data


def default_model_capability_db_path(quality_db_path: str) -> str:
    """Return the persistent modality DB path beside the quality DB."""
    configured = os.environ.get("TUSKER_MODEL_CAPABILITY_DB_PATH", "").strip()
    if configured:
        return configured
    if quality_db_path == ":memory:":
        return ":memory:"
    return str(Path(quality_db_path).with_name("model_capability.db"))


class ModelCapabilityDB:
    """SQLite store for the latest capability evidence per model."""

    def __init__(self, path: str):
        self.path = str(path)
        self._memory_connection: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._memory_connection = sqlite3.connect(self.path)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_db()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._memory_connection or sqlite3.connect(
            self.path, timeout=30
        )
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
        finally:
            if connection is not self._memory_connection:
                connection.close()

    def _ensure_db(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_capability (
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    probe_version TEXT NOT NULL,
                    http_status INTEGER,
                    latency_ms REAL,
                    failure_class TEXT,
                    checked_at REAL NOT NULL,
                    PRIMARY KEY (provider, model, capability)
                )
                """
            )
            connection.commit()

    def record(
        self,
        *,
        provider: str,
        model: str,
        capability: str,
        status: str,
        source: str,
        probe_version: str = MODEL_CAPABILITY_PROBE_VERSION,
        http_status: int | None = None,
        latency_ms: float | None = None,
        failure_class: str | None = None,
        checked_at: float | None = None,
    ) -> ModelCapability:
        """Upsert one normalized evidence record."""
        normalized_capability = normalize_capability(capability)
        normalized_status = str(status).strip().lower()
        if not normalized_capability:
            raise ValueError("capability must not be empty")
        if normalized_status not in CAPABILITY_STATUSES:
            raise ValueError(f"unknown capability status: {status}")
        result = ModelCapability(
            provider=str(provider).strip().lower().replace("_", "-"),
            model=str(model).strip(),
            capability=normalized_capability,
            status=normalized_status,
            source=str(source).strip().lower() or "unknown",
            probe_version=str(probe_version),
            http_status=int(http_status) if http_status is not None else None,
            latency_ms=(float(latency_ms) if latency_ms is not None else None),
            failure_class=(str(failure_class).strip() if failure_class else None),
            checked_at=time.time() if checked_at is None else float(checked_at),
        )
        if not result.provider or not result.model:
            raise ValueError("provider and model must not be empty")
        with self._connection() as connection:
            existing_row = connection.execute(
                """
                SELECT provider, model, capability, status, source,
                       probe_version, http_status, latency_ms, failure_class,
                       checked_at
                FROM model_capability
                WHERE provider = ? AND model = ? AND capability = ?
                """,
                (result.provider, result.model, result.capability),
            ).fetchone()
            # A catalog refresh or capability discovery must not downgrade a
            # concrete live probe. A later explicit probe can replace either
            # result, including a newly discovered recovery.
            if (
                existing_row is not None
                and str(existing_row[3]) in {"passed", "unsupported"}
                and result.status in {"advertised", "discovered"}
            ):
                return self._from_row(existing_row)
            connection.execute(
                """
                INSERT INTO model_capability (
                    provider, model, capability, status, source, probe_version,
                    http_status, latency_ms, failure_class, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, model, capability) DO UPDATE SET
                    status=excluded.status,
                    source=excluded.source,
                    probe_version=excluded.probe_version,
                    http_status=excluded.http_status,
                    latency_ms=excluded.latency_ms,
                    failure_class=excluded.failure_class,
                    checked_at=excluded.checked_at
                """,
                (
                    result.provider,
                    result.model,
                    result.capability,
                    result.status,
                    result.source,
                    result.probe_version,
                    result.http_status,
                    result.latency_ms,
                    result.failure_class,
                    result.checked_at,
                ),
            )
            connection.commit()
        return result

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> ModelCapability:
        return ModelCapability(
            provider=str(row[0]),
            model=str(row[1]),
            capability=str(row[2]),
            status=str(row[3]),
            source=str(row[4]),
            probe_version=str(row[5]),
            http_status=int(row[6]) if row[6] is not None else None,
            latency_ms=float(row[7]) if row[7] is not None else None,
            failure_class=str(row[8]) if row[8] else None,
            checked_at=float(row[9]),
        )

    def get(
        self,
        provider: str,
        model: str,
        capability: str,
    ) -> ModelCapability | None:
        """Return the latest record for a concrete capability."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT provider, model, capability, status, source,
                       probe_version, http_status, latency_ms, failure_class,
                       checked_at
                FROM model_capability
                WHERE provider = ? AND model = ? AND capability = ?
                """,
                (
                    str(provider).strip().lower().replace("_", "-"),
                    str(model).strip(),
                    normalize_capability(capability),
                ),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def for_model(self, provider: str, model: str) -> list[ModelCapability]:
        """Return all capability records for one provider/model."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT provider, model, capability, status, source,
                       probe_version, http_status, latency_ms, failure_class,
                       checked_at
                FROM model_capability
                WHERE provider = ? AND model = ?
                ORDER BY capability
                """,
                (
                    str(provider).strip().lower().replace("_", "-"),
                    str(model).strip(),
                ),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def records(self) -> list[ModelCapability]:
        """Return all records in stable provider/model/capability order."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT provider, model, capability, status, source,
                       probe_version, http_status, latency_ms, failure_class,
                       checked_at
                FROM model_capability
                ORDER BY provider, model, capability
                """
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def status(self) -> dict[str, object]:
        """Return a compact, credential-safe summary for /status."""
        records = self.records()
        by_status: dict[str, int] = {}
        by_source: dict[str, int] = {}
        by_capability: dict[str, dict[str, int]] = {}
        for record in records:
            by_status[record.status] = by_status.get(record.status, 0) + 1
            by_source[record.source] = by_source.get(record.source, 0) + 1
            capability_counts = by_capability.setdefault(record.capability, {})
            capability_counts[record.status] = (
                capability_counts.get(record.status, 0) + 1
            )
        verified_models = len({
            (record.provider, record.model)
            for record in records
            if record.verified
        })
        return {
            "probe_version": MODEL_CAPABILITY_PROBE_VERSION,
            "total_records": len(records),
            "models": len({(record.provider, record.model) for record in records}),
            "verified_models": verified_models,
            "by_status": by_status,
            "by_source": by_source,
            "by_capability": by_capability,
        }


__all__ = [
    "CAPABILITY_STATUSES",
    "ENDPOINT_CAPABILITIES",
    "INPUT_MODALITY_CAPABILITIES",
    "KNOWN_CAPABILITIES",
    "MODEL_CAPABILITY_PROBE_VERSION",
    "ModelCapability",
    "ModelCapabilityDB",
    "OUTPUT_MODALITY_CAPABILITIES",
    "default_model_capability_db_path",
    "normalize_capability",
]
