"""Bounded operator-controlled probe coordination.

This is an application-level guard, not a distributed rate limiter.
Production schedulers must also enforce shared-cluster quotas and approvals.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


class ProbeCoordinator:
    def __init__(self, *, max_concurrent: int = 2, cooldown_seconds: float = 300):
        if not (1 <= max_concurrent <= 32) or cooldown_seconds < 0:
            raise ValueError("invalid probe limits")
        self._sem = asyncio.Semaphore(max_concurrent)
        self._cooldown = cooldown_seconds
        self._last: dict[tuple[str, str, str], float] = {}
        self._lock = asyncio.Lock()

    async def run(
        self, *, provider: str, endpoint_id: str, model: str,
        approved: bool, operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        if not approved:
            raise PermissionError("protocol probes require operator approval")
        if not (provider and endpoint_id and model):
            raise ValueError("probe identity must be complete")
        key = (provider.lower(), endpoint_id, model)
        async with self._sem:
            async with self._lock:
                now = time.monotonic()
                previous = self._last.get(key)
                if previous is not None and now - previous < self._cooldown:
                    raise RuntimeError("endpoint probe cooldown is active")
                # Reserve before making any billable upstream call.
                self._last[key] = now
            return await operation()
