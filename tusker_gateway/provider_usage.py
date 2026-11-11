"""Provider capacity protection and persisted usage accounting.

The provider can reject a request because its worker pool is full even when
the HTTP request itself is otherwise valid.  This module keeps that condition
from becoming a retry storm and records enough usage data to diagnose it
without retaining request bodies or credentials.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tusker_gateway.storage import shared_database

logger = logging.getLogger(__name__)

NVIDIA_CAPACITY_GROUP = "nvidia"
LOCAL_LLM_CAPACITY_GROUP = "local-llm"
MLX_MAC_CAPACITY_GROUP = "mlx-mac"
OLLAMA_CLOUD_CAPACITY_GROUP = "ollama-cloud"
SYNTHETIC_CAPACITY_GROUP = "synthetic"
CLI_CAPACITY_GROUP = "cli"
DEFAULT_NVIDIA_MAX_CONCURRENT = 8
DEFAULT_LOCAL_LLM_MAX_CONCURRENT = 1
DEFAULT_MLX_MAC_MAX_CONCURRENT = 1
DEFAULT_OLLAMA_CLOUD_MAX_CONCURRENT = 3
DEFAULT_SYNTHETIC_MAX_CONCURRENT = 4
DEFAULT_CLI_MAX_CONCURRENT = 1
DEFAULT_CAPACITY_COOLDOWN_SECS = 300.0
DEFAULT_CAPACITY_BUSY_COOLDOWN_SECS = 2.0
# Queued admission is opt-in per group through ``TUSKER_<GROUP>_QUEUE_DEPTH``.
# A single-request local backend rejects far more often than it is genuinely
# unreachable: on 2026-11-11 ``mlx-mac/qwen3.8-27b`` recorded 77 capacity
# rejections across 80 privacy-pool attempts while the model itself answered
# in 7.1s cold and 1.7s warm. Waiting for a slot is the right behaviour for a
# backend that is merely busy; rejecting is right only when the wait would
# outlast the caller's own budget.
DEFAULT_CAPACITY_QUEUE_DEPTH = 0
DEFAULT_CAPACITY_QUEUE_WAIT_SECS = 60.0
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
    Nvidia requests use the ``nvidia`` provider. Local providers are isolated
    separately because each backend has its own single-request worker budget.

    Process-backed providers (``opencode-cli``, ``claude-code-cli``) share one
    budget because each in-flight request runs a CLI child process inside the
    gateway container: an uncapped burst exhausts the pod memory limit and the
    kernel OOM-kills the gateway. ``kilo-cli`` only counts when it is not
    proxied to the dedicated worker, which bounds its own subprocesses.
    """
    provider_name = str(provider or "").strip().lower()
    model_name = str(model or "").strip().lower()
    detail_text = str(detail or "")
    if provider_name == LOCAL_LLM_CAPACITY_GROUP:
        return LOCAL_LLM_CAPACITY_GROUP
    if provider_name == MLX_MAC_CAPACITY_GROUP:
        return MLX_MAC_CAPACITY_GROUP
    if provider_name == OLLAMA_CLOUD_CAPACITY_GROUP:
        return OLLAMA_CLOUD_CAPACITY_GROUP
    if provider_name == SYNTHETIC_CAPACITY_GROUP:
        return SYNTHETIC_CAPACITY_GROUP
    if provider_name in ("opencode-cli", "claude-code-cli"):
        return CLI_CAPACITY_GROUP
    if (
        provider_name == "kilo-cli"
        and not os.environ.get("TUSKER_KILO_WORKER_URL", "").strip()
    ):
        return CLI_CAPACITY_GROUP
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
    settings = {
        NVIDIA_CAPACITY_GROUP: (
            "TUSKER_NVIDIA_MAX_CONCURRENT", DEFAULT_NVIDIA_MAX_CONCURRENT
        ),
        LOCAL_LLM_CAPACITY_GROUP: (
            "TUSKER_LOCAL_LLM_MAX_CONCURRENT", DEFAULT_LOCAL_LLM_MAX_CONCURRENT
        ),
        MLX_MAC_CAPACITY_GROUP: (
            "TUSKER_MLX_MAC_MAX_CONCURRENT", DEFAULT_MLX_MAC_MAX_CONCURRENT
        ),
        OLLAMA_CLOUD_CAPACITY_GROUP: (
            "TUSKER_OLLAMA_CLOUD_MAX_CONCURRENT", DEFAULT_OLLAMA_CLOUD_MAX_CONCURRENT
        ),
        SYNTHETIC_CAPACITY_GROUP: (
            "TUSKER_SYNTHETIC_MAX_CONCURRENT", DEFAULT_SYNTHETIC_MAX_CONCURRENT
        ),
        CLI_CAPACITY_GROUP: (
            "TUSKER_CLI_MAX_CONCURRENT", DEFAULT_CLI_MAX_CONCURRENT
        ),
    }
    setting = settings.get(group)
    if setting is None:
        return 0
    env_name, default = setting
    raw = os.environ.get(env_name, str(default))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def capacity_queue_depth(group: str) -> int:
    """Return how many waiters a saturated group may hold; zero disables queueing.

    Queued admission trades a bounded wait for a fallback attempt. It is only
    worth it when the backend is busy rather than broken and the caller's own
    budget still has room, so it stays opt-in per capacity group.
    """
    raw = os.environ.get(
        f"TUSKER_{group.upper().replace('-', '_')}_QUEUE_DEPTH",
        str(DEFAULT_CAPACITY_QUEUE_DEPTH),
    )
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_CAPACITY_QUEUE_DEPTH


def capacity_queue_wait_seconds(group: str) -> float:
    """Return the longest a waiter may hold a queue slot before giving up."""
    raw = os.environ.get(
        f"TUSKER_{group.upper().replace('-', '_')}_QUEUE_WAIT_SECS",
        str(DEFAULT_CAPACITY_QUEUE_WAIT_SECS),
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_CAPACITY_QUEUE_WAIT_SECS


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
    """Process-local concurrency accounting by provider group.

    Admission is fail-fast by default. A group with
    ``TUSKER_<GROUP>_QUEUE_DEPTH`` set also holds up to that many waiters in
    arrival order, and each released slot is taken by the oldest waiter
    instead of being handed away by ``release``. The wait is bounded by
    ``TUSKER_<GROUP>_QUEUE_WAIT_SECS`` so a genuinely stuck backend still
    yields to the next pool candidate.

    ``_active`` is only ever incremented by a successful slot claim and only
    decremented by ``release``. Queueing therefore cannot double-release a
    slot, which handoff futures would have to guard against on every timeout
    and cancellation path.
    """

    # Waiters poll rather than receive a direct handoff: a released slot is
    # claimed by the oldest live waiter on its next tick. The ceiling keeps
    # queue latency well under a second while a 1.7-7s local generation runs.
    _POLL_SECS = 0.1

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}
        self._rejections: dict[str, int] = {}
        self._waiters: dict[str, list[int]] = {}
        self._queue_wait_timeouts: dict[str, int] = {}
        self._next_ticket = 0

    def _try_take(self, group: str) -> CapacityLease | None:
        """Claim a slot without counting a rejection when none is free."""
        with self._lock:
            active = self._active.get(group, 0)
            if active >= capacity_limit(group):
                return None
            self._active[group] = active + 1
        return CapacityLease(self, group)

    def _record_rejection(self, group: str) -> None:
        with self._lock:
            self._rejections[group] = self._rejections.get(group, 0) + 1

    def acquire(self, group: str) -> CapacityLease | None:
        """Reserve a slot or return ``None`` without waiting."""
        if capacity_limit(group) <= 0:
            return CapacityLease(self, group, counted=False)
        lease = self._try_take(group)
        if lease is None:
            self._record_rejection(group)
        return lease

    async def acquire_queued(self, group: str) -> CapacityLease | None:
        """Reserve a slot, waiting in arrival order when the group is saturated.

        Returns ``None`` exactly like a fail-fast rejection: queueing is
        disabled for the group, the queue is already full, or the wait
        outlasted the group's wait budget. An unlimited group never queues.
        """
        if capacity_limit(group) <= 0:
            return CapacityLease(self, group, counted=False)
        depth = capacity_queue_depth(group)
        # Claim-or-enqueue is one critical section so a fresh arrival can never
        # take a slot ahead of waiters that are already queued.
        ticket = self._claim_or_enqueue(group, depth)
        if isinstance(ticket, CapacityLease):
            return ticket
        if ticket is None:
            self._record_rejection(group)
            return None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + capacity_queue_wait_seconds(group)
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self._record_queue_timeout(group)
                    self._record_rejection(group)
                    logger.info(
                        "capacity queue wait expired group=%s waiters=%d",
                        group,
                        self.waiting(group),
                    )
                    return None
                lease = self._take_when_head(group, ticket)
                if lease is not None:
                    return lease
                await asyncio.sleep(min(self._POLL_SECS, remaining))
        finally:
            self._unregister_waiter(group, ticket)

    def _claim_or_enqueue(
        self, group: str, depth: int
    ) -> "CapacityLease | int | None":
        """Take a free slot, else queue; ``None`` when the queue is full."""
        with self._lock:
            active = self._active.get(group, 0)
            if active < capacity_limit(group):
                self._active[group] = active + 1
                return CapacityLease(self, group)
            if depth <= 0:
                return None
            waiters = self._waiters.setdefault(group, [])
            if len(waiters) >= depth:
                if not waiters:
                    self._waiters.pop(group, None)
                return None
            ticket = self._next_ticket
            self._next_ticket += 1
            waiters.append(ticket)
            return ticket

    def _take_when_head(self, group: str, ticket: int) -> CapacityLease | None:
        """Claim a slot only if ``ticket`` is the oldest waiter queued."""
        with self._lock:
            waiters = self._waiters.get(group)
            if not waiters or waiters[0] != ticket:
                return None
            active = self._active.get(group, 0)
            if active >= capacity_limit(group):
                return None
            self._active[group] = active + 1
            return CapacityLease(self, group)

    def _unregister_waiter(self, group: str, ticket: int) -> None:
        with self._lock:
            waiters = self._waiters.get(group)
            if waiters is None:
                return
            try:
                waiters.remove(ticket)
            except ValueError:
                return
            if not waiters:
                self._waiters.pop(group, None)

    def _record_queue_timeout(self, group: str) -> None:
        with self._lock:
            self._queue_wait_timeouts[group] = self._queue_wait_timeouts.get(group, 0) + 1

    def waiting(self, group: str) -> int:
        """Return how many requests are queued for ``group`` right now."""
        with self._lock:
            return len(self._waiters.get(group, ()))

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
            groups = (
                set(self._active)
                | set(self._rejections)
                | set(self._waiters)
                | set(self._queue_wait_timeouts)
            )
            return {
                group: {
                    "active": self._active.get(group, 0),
                    "limit": capacity_limit(group),
                    "rejections": self._rejections.get(group, 0),
                    "queued": len(self._waiters.get(group, ())),
                    "queue_depth": capacity_queue_depth(group),
                    "queue_wait_timeouts": self._queue_wait_timeouts.get(group, 0),
                }
                for group in sorted(groups)
            }

    def reset(self) -> None:
        """Clear process-local state; intended for isolated test processes."""
        with self._lock:
            self._active.clear()
            self._rejections.clear()
            self._waiters.clear()
            self._queue_wait_timeouts.clear()


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
        self._db = shared_database(path)
        if not self._db.is_postgres and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_db()

    def _connection(self):
        return self._db.connection()

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
            current = "provider_usage_daily." if getattr(connection, "is_postgres", False) else ""
            connection.execute(
                f"""
                INSERT INTO provider_usage_daily (
                    usage_day, group_name, provider, model, requests,
                    successes, failures, capacity_rejections, prompt_tokens,
                    completion_tokens, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(usage_day, group_name, provider, model) DO UPDATE SET
                    requests = {current}requests + 1,
                    successes = {current}successes + excluded.successes,
                    failures = {current}failures + excluded.failures,
                    capacity_rejections = {current}capacity_rejections + excluded.capacity_rejections,
                    prompt_tokens = {current}prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = {current}completion_tokens + excluded.completion_tokens,
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
    "CLI_CAPACITY_GROUP",
    "DEFAULT_CAPACITY_COOLDOWN_SECS",
    "DEFAULT_CLI_MAX_CONCURRENT",
    "DEFAULT_LOCAL_LLM_MAX_CONCURRENT",
    "DEFAULT_MLX_MAC_MAX_CONCURRENT",
    "DEFAULT_NVIDIA_MAX_CONCURRENT",
    "LOCAL_LLM_CAPACITY_GROUP",
    "MLX_MAC_CAPACITY_GROUP",
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
