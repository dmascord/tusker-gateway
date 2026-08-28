"""Provider capacity protection and persisted usage accounting.

The provider can reject a request because its worker pool is full even when
the HTTP request itself is otherwise valid.  This module keeps that condition
from becoming a retry storm and records enough usage data to diagnose it
without retaining request bodies or credentials.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


NVIDIA_CAPACITY_GROUP = "nvidia"
DEFAULT_NVIDIA_MAX_CONCURRENT = 8
DEFAULT_CAPACITY_COOLDOWN_SECS = 300.0
DEFAULT_CAPACITY_BUSY_COOLDOWN_SECS = 2.0

_CAPACITY_HINTS = (
    "resourceexhausted",
    "worker local total request limit",
    "too many concurrent",
    "concurrency limit",
    "capacity exhausted",
)
_NVIDIA_HINT_RE = re.compile(
    r"(?:provider[\"']?\s*:\s*[\"']?nvidia|from\s+nvidia|\bnvidia\b)",
    re.IGNORECASE,
)


def is_capacity_error(value: object) -> bool:
    """Return whether an upstream detail indicates worker saturation."""
    text = str(value or "").lower()
    return any(hint in text for hint in _CAPACITY_HINTS)


def capacity_group_for_route(
    provider: str,
    model: str,
    detail: object = "",
) -> str | None:
    """Map a route or provider error to a shared capacity group.

    OpenRouter exposes Nvidia models under ``nvidia/<model>`` while direct
    Nvidia requests use the ``nvidia`` provider.  They consume the same
    logical upstream capacity from this gateway's point of view.
    """
    provider_name = str(provider or "").strip().lower()
    model_name = str(model or "").strip().lower()
    detail_text = str(detail or "")
    if (
        provider_name == NVIDIA_CAPACITY_GROUP
        or model_name.startswith("nvidia/")
        or (is_capacity_error(detail_text) and _NVIDIA_HINT_RE.search(detail_text))
    ):
        return NVIDIA_CAPACITY_GROUP
    return None


def capacity_cooldown_seconds(detail: object = "") -> float:
    """Return the quarantine window for a provider capacity failure."""
    raw = os.environ.get(
        "TUSKER_PROVIDER_CAPACITY_COOLDOWN_SECS",
        str(DEFAULT_CAPACITY_COOLDOWN_SECS),
    )
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_CAPACITY_COOLDOWN_SECS


def capacity_busy_cooldown_seconds() -> float:
    """Return the brief backoff used when this process is already saturated."""
    raw = os.environ.get(
        "TUSKER_PROVIDER_CAPACITY_BUSY_COOLDOWN_SECS",
        str(DEFAULT_CAPACITY_BUSY_COOLDOWN_SECS),
    )
    try:
        return max(0.1, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_CAPACITY_BUSY_COOLDOWN_SECS


def capacity_limit(group: str) -> int:
    """Return the local concurrency ceiling; zero means unlimited."""
    if group == NVIDIA_CAPACITY_GROUP:
        raw = os.environ.get(
            "TUSKER_NVIDIA_MAX_CONCURRENT",
            str(DEFAULT_NVIDIA_MAX_CONCURRENT),
        )
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_NVIDIA_MAX_CONCURRENT
    return 0


@dataclass
class CapacityLease:
    """An idempotent reservation for one provider request."""

    _controller: "CapacityController"
    group: str
    counted: bool = True
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.counted:
            self._controller.release(self.group)


class CapacityController:
    """Process-local, fail-fast concurrency accounting by provider group."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}
        self._rejections: dict[str, int] = {}

    def acquire(self, group: str) -> CapacityLease | None:
        """Reserve a slot or return ``None`` without waiting."""
        limit = capacity_limit(group)
        if limit <= 0:
            return CapacityLease(self, group, counted=False)
        with self._lock:
            active = self._active.get(group, 0)
            if active >= limit:
                self._rejections[group] = self._rejections.get(group, 0) + 1
                return None
            self._active[group] = active + 1
        return CapacityLease(self, group)

    def release(self, group: str) -> None:
        with self._lock:
            active = self._active.get(group, 0)
            if active <= 1:
                self._active.pop(group, None)
            else:
                self._active[group] = active - 1

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Return bounded process-local capacity counters."""
        with self._lock:
            groups = set(self._active) | set(self._rejections)
            return {
                group: {
                    "active": self._active.get(group, 0),
                    "limit": capacity_limit(group),
                    "rejections": self._rejections.get(group, 0),
                }
                for group in sorted(groups)
            }

    def reset(self) -> None:
        """Clear process-local state; intended for isolated test processes."""
        with self._lock:
            self._active.clear()
            self._rejections.clear()


_CAPACITY_CONTROLLER = CapacityController()


def capacity_controller() -> CapacityController:
    """Return the process-wide provider capacity controller."""
    return _CAPACITY_CONTROLLER


def default_provider_usage_db_path(quality_db_path: str) -> str:
    """Return the usage ledger path beside the transport-quality database."""
    configured = os.environ.get("TUSKER_PROVIDER_USAGE_DB_PATH", "").strip()
    if configured:
        return configured
    if quality_db_path == ":memory:":
        return ":memory:"
    return str(Path(quality_db_path).with_name("provider_usage.db"))


class ProviderUsageDB:
    """Daily provider/model counters without request content or credentials."""

    def __init__(self, path: str):
        self.path = path
        self._memory_connection: sqlite3.Connection | None = None
        if path == ":memory:":
            self._memory_connection = sqlite3.connect(path)
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_db()

    def _connection(self) -> sqlite3.Connection:
        return self._memory_connection or sqlite3.connect(self.path, timeout=30)

    def _ensure_db(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_usage_daily (
                    usage_day TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    requests INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    capacity_rejections INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (usage_day, group_name, provider, model)
                )
                """
            )
            connection.commit()

    def record(
        self,
        *,
        provider: str,
        model: str,
        group: str | None = None,
        success: bool,
        capacity_rejected: bool = False,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        usage_day: str | None = None,
    ) -> None:
        """Add one request outcome to today's aggregate row."""
        day = usage_day or time.strftime("%Y-%m-%d", time.gmtime())
        group_name = group or provider
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_usage_daily (
                    usage_day, group_name, provider, model, requests,
                    successes, failures, capacity_rejections, prompt_tokens,
                    completion_tokens, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(usage_day, group_name, provider, model) DO UPDATE SET
                    requests = requests + 1,
                    successes = successes + excluded.successes,
                    failures = failures + excluded.failures,
                    capacity_rejections = capacity_rejections + excluded.capacity_rejections,
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    updated_at = excluded.updated_at
                """,
                (
                    day,
                    group_name,
                    str(provider),
                    str(model),
                    int(bool(success)),
                    int(not success),
                    int(capacity_rejected),
                    max(0, int(prompt_tokens)),
                    max(0, int(completion_tokens)),
                    now,
                ),
            )
            connection.commit()

    def status(self) -> dict[str, Any]:
        """Return today's totals grouped by logical provider capacity."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT group_name, SUM(requests), SUM(successes), SUM(failures),
                       SUM(capacity_rejections), SUM(prompt_tokens),
                       SUM(completion_tokens)
                FROM provider_usage_daily
                WHERE usage_day = ?
                GROUP BY group_name
                ORDER BY group_name
                """,
                (day,),
            ).fetchall()
        groups: dict[str, dict[str, int]] = {}
        for row in rows:
            groups[str(row[0])] = {
                "requests": int(row[1] or 0),
                "successes": int(row[2] or 0),
                "failures": int(row[3] or 0),
                "capacity_rejections": int(row[4] or 0),
                "prompt_tokens": int(row[5] or 0),
                "completion_tokens": int(row[6] or 0),
            }
        totals = {
            key: sum(group[key] for group in groups.values())
            for key in (
                "requests",
                "successes",
                "failures",
                "capacity_rejections",
                "prompt_tokens",
                "completion_tokens",
            )
        }
        return {"usage_day": day, "groups": groups, "totals": totals}


__all__ = [
    "CapacityController",
    "CapacityLease",
    "DEFAULT_CAPACITY_COOLDOWN_SECS",
    "DEFAULT_NVIDIA_MAX_CONCURRENT",
    "NVIDIA_CAPACITY_GROUP",
    "ProviderUsageDB",
    "capacity_busy_cooldown_seconds",
    "capacity_controller",
    "capacity_cooldown_seconds",
    "capacity_group_for_route",
    "capacity_limit",
    "default_provider_usage_db_path",
    "is_capacity_error",
]
