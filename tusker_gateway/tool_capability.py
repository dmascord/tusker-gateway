"""Persisted behavioral tool-call capability results.

``quality.py`` records transport health. This module records whether a model
actually satisfies the gateway's streaming tool-call contract, so a provider
that returns HTTP 200 with unusable prose cannot look healthy by accident.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import IntEnum
from pathlib import Path
from typing import Iterator


TOOL_CAPABILITY_PROBE_VERSION = "stream-tool-contract-v1"


class ToolCapabilityLevel(IntEnum):
    """Behavior observed at the public OpenAI streaming boundary."""

    UNKNOWN = 0
    UNAVAILABLE = 1
    UNSUPPORTED = 2
    STRUCTURED_STREAM = 3
    STRICT_STRUCTURED_STREAM = 4


QUALIFIED_TOOL_LEVELS = frozenset({
    ToolCapabilityLevel.STRICT_STRUCTURED_STREAM,
})


@dataclass(frozen=True)
class ToolCapability:
    """Latest behavioral qualification result for one provider/model pair."""

    provider: str
    model: str
    level: ToolCapabilityLevel
    status: str
    probe_version: str
    http_status: int | None
    tool_call_count: int
    structured_stream: bool
    arguments_valid: bool
    arguments_match: bool
    finish_reason: str | None
    unexpected_text: bool
    latency_ms: float | None
    failure_class: str | None
    checked_at: float

    @property
    def level_name(self) -> str:
        return self.level.name.lower()

    @property
    def qualified_for_tools(self) -> bool:
        """Whether the result is safe for a streaming tool-bearing request."""
        return self.probe_version == TOOL_CAPABILITY_PROBE_VERSION and self.level in QUALIFIED_TOOL_LEVELS

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["level"] = self.level_name
        data["qualified_for_tools"] = self.qualified_for_tools
        return data


def default_tool_capability_db_path(quality_db_path: str) -> str:
    """Return the capability DB path beside the transport-quality DB."""
    configured = os.environ.get("TUSKER_TOOL_CAPABILITY_DB_PATH", "").strip()
    if configured:
        return configured
    if quality_db_path == ":memory:":
        return ":memory:"
    return str(Path(quality_db_path).with_name("model_tool_capability.db"))


class ToolCapabilityDB:
    """SQLite store for the latest qualification result per model."""

    def __init__(self, path: str):
        self.path = path
        self._memory_connection: sqlite3.Connection | None = None
        if path == ":memory:":
            self._memory_connection = sqlite3.connect(path)
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_db()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._memory_connection or sqlite3.connect(
            self.path, timeout=30
        )
        try:
            yield connection
        finally:
            if connection is not self._memory_connection:
                connection.close()

    def _ensure_db(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_tool_capability (
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    probe_version TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    http_status INTEGER,
                    tool_call_count INTEGER NOT NULL DEFAULT 0,
                    structured_stream INTEGER NOT NULL DEFAULT 0,
                    arguments_valid INTEGER NOT NULL DEFAULT 0,
                    arguments_match INTEGER NOT NULL DEFAULT 0,
                    finish_reason TEXT,
                    unexpected_text INTEGER NOT NULL DEFAULT 0,
                    latency_ms REAL,
                    failure_class TEXT,
                    checked_at REAL NOT NULL,
                    PRIMARY KEY (provider, model)
                )
                """
            )
            connection.commit()

    def record(
        self,
        *,
        provider: str,
        model: str,
        level: ToolCapabilityLevel,
        status: str,
        probe_version: str = TOOL_CAPABILITY_PROBE_VERSION,
        http_status: int | None = None,
        tool_call_count: int = 0,
        structured_stream: bool = False,
        arguments_valid: bool = False,
        arguments_match: bool = False,
        finish_reason: str | None = None,
        unexpected_text: bool = False,
        latency_ms: float | None = None,
        failure_class: str | None = None,
        checked_at: float | None = None,
    ) -> ToolCapability:
        """Upsert a result and return the normalized record."""
        normalized_level = ToolCapabilityLevel(level)
        checked = time.time() if checked_at is None else float(checked_at)
        result = ToolCapability(
            provider=str(provider),
            model=str(model),
            level=normalized_level,
            status=str(status),
            probe_version=str(probe_version),
            http_status=http_status,
            tool_call_count=max(0, int(tool_call_count)),
            structured_stream=bool(structured_stream),
            arguments_valid=bool(arguments_valid),
            arguments_match=bool(arguments_match),
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            unexpected_text=bool(unexpected_text),
            latency_ms=float(latency_ms) if latency_ms is not None else None,
            failure_class=str(failure_class) if failure_class else None,
            checked_at=checked,
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO model_tool_capability (
                    provider, model, probe_version, level, status, http_status,
                    tool_call_count, structured_stream, arguments_valid,
                    arguments_match, finish_reason, unexpected_text,
                    latency_ms, failure_class, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                    probe_version=excluded.probe_version,
                    level=excluded.level,
                    status=excluded.status,
                    http_status=excluded.http_status,
                    tool_call_count=excluded.tool_call_count,
                    structured_stream=excluded.structured_stream,
                    arguments_valid=excluded.arguments_valid,
                    arguments_match=excluded.arguments_match,
                    finish_reason=excluded.finish_reason,
                    unexpected_text=excluded.unexpected_text,
                    latency_ms=excluded.latency_ms,
                    failure_class=excluded.failure_class,
                    checked_at=excluded.checked_at
                """,
                (
                    result.provider,
                    result.model,
                    result.probe_version,
                    int(result.level),
                    result.status,
                    result.http_status,
                    result.tool_call_count,
                    int(result.structured_stream),
                    int(result.arguments_valid),
                    int(result.arguments_match),
                    result.finish_reason,
                    int(result.unexpected_text),
                    result.latency_ms,
                    result.failure_class,
                    result.checked_at,
                ),
            )
            connection.commit()
        return result

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> ToolCapability:
        return ToolCapability(
            provider=str(row[0]),
            model=str(row[1]),
            probe_version=str(row[2]),
            level=ToolCapabilityLevel(int(row[3])),
            status=str(row[4]),
            http_status=int(row[5]) if row[5] is not None else None,
            tool_call_count=int(row[6]),
            structured_stream=bool(row[7]),
            arguments_valid=bool(row[8]),
            arguments_match=bool(row[9]),
            finish_reason=str(row[10]) if row[10] is not None else None,
            unexpected_text=bool(row[11]),
            latency_ms=float(row[12]) if row[12] is not None else None,
            failure_class=str(row[13]) if row[13] else None,
            checked_at=float(row[14]),
        )

    def get(self, provider: str, model: str) -> ToolCapability | None:
        """Return the latest result, or ``None`` when never probed."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT provider, model, probe_version, level, status,
                       http_status, tool_call_count, structured_stream,
                       arguments_valid, arguments_match, finish_reason,
                       unexpected_text, latency_ms, failure_class, checked_at
                FROM model_tool_capability
                WHERE provider = ? AND model = ?
                """,
                (provider, model),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def records(self) -> list[ToolCapability]:
        """Return all latest records in stable provider/model order."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT provider, model, probe_version, level, status,
                       http_status, tool_call_count, structured_stream,
                       arguments_valid, arguments_match, finish_reason,
                       unexpected_text, latency_ms, failure_class, checked_at
                FROM model_tool_capability
                ORDER BY provider, model
                """
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def is_qualified(self, provider: str, model: str) -> bool:
        """Return whether the current probe version passed the tool gate."""
        result = self.get(provider, model)
        return result is not None and result.qualified_for_tools

    def status(self) -> dict[str, object]:
        """Return a compact summary suitable for status/debug endpoints."""
        records = self.records()
        levels: dict[str, int] = {}
        for record in records:
            levels[record.level_name] = levels.get(record.level_name, 0) + 1
        return {
            "probe_version": TOOL_CAPABILITY_PROBE_VERSION,
            "total_models": len(records),
            "qualified_models": sum(record.qualified_for_tools for record in records),
            "levels": levels,
        }


__all__ = [
    "QUALIFIED_TOOL_LEVELS",
    "TOOL_CAPABILITY_PROBE_VERSION",
    "ToolCapability",
    "ToolCapabilityDB",
    "ToolCapabilityLevel",
    "default_tool_capability_db_path",
]
