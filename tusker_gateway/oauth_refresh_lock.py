"""Cross-process lock for OAuth token refresh.

With more than one gateway replica, two rotators can independently notice the
same near-expiry Codex credential. Both would call the OAuth endpoint with the
same single-use refresh token; one succeeds and rotates the token, the other
gets ``refresh_token_reused`` and marks the credential permanently failed for
24 h (``passthrough.CodexTokenRotator._PERMANENT_REFRESH_ERROR_CODES``).

The in-process ``asyncio.Lock`` only serializes a single replica. The DB CAS
persisted after the refresh is too late: the upstream token has already been
consumed by then. This module provides a **session-level PostgreSQL advisory
lock** held across adopt → refresh → persist, which is the only way to make
that sequence atomic across processes.

Behaviour:

- PostgreSQL: ``SELECT pg_advisory_lock(key)`` / ``pg_advisory_unlock(key)`` on
  a dedicated pooled connection for the duration of the ``async with``.
- SQLite / no managed DB: a process-local ``asyncio.Lock`` keyed by provider
  and credential identity. This preserves single-process correctness (which
  the rotator already had via its own lock) and is a no-op for HA, which is
  impossible without a shared store anyway.
- Any lock acquisition failure is fail-open with a warning: a gateway that
  cannot acquire the coordination lock must still be able to refresh tokens,
  rather than lock every replica out of auth.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

logger = logging.getLogger(__name__)

_LOCK_NAMESPACE = 0x5455534B  # "TUSK" - keeps keys disjoint from other apps
_locks: dict[int, asyncio.Lock] = {}
_locks_guard = threading.Lock()
_metrics_registry: Any | None = None


def bind_metrics_registry(registry: Any | None) -> None:
    """Bind the metrics registry used for fail-open lock events."""
    global _metrics_registry
    _metrics_registry = registry


def _count_unavailable(provider: str) -> None:
    registry = _metrics_registry
    counter = getattr(registry, "oauth_refresh_lock_unavailable", None)
    if counter is not None:
        counter.inc({"provider": provider})


def _lock_key(provider: str, identity: str) -> int:
    """Stable 64-bit advisory-lock key for a credential identity."""
    digest = hashlib.sha256(f"{provider}:{identity}".encode("utf-8")).digest()
    return _LOCK_NAMESPACE << 32 | int.from_bytes(digest[:4], "big")


class RefreshLock:
    """Wrap a DB handle with a cross-process credential-refresh lock."""

    def __init__(self, *, acquire: Callable[[str], Any] | None = None) -> None:
        # ``acquire`` returns the store context manager. Keeping this callback
        # injected avoids importing config_store and creating a cycle.
        self._acquire = acquire

    @asynccontextmanager
    async def guard(self, provider: str, identity: str) -> AsyncIterator[None]:
        """Hold the lock; fail open only when acquisition itself fails."""
        key = _lock_key(provider, identity)
        if self._acquire is None:
            async with self._local_lock(key):
                yield
            return

        lock_cm = self._acquire(key)
        try:
            await lock_cm.__aenter__()
        except Exception:  # noqa: BLE001 - fail open, never block auth
            # __aenter__ may have acquired a session-level advisory lock before
            # failing. Always invoke __aexit__ so it cannot leak to the pool.
            try:
                await lock_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - cleanup cannot block auth
                logger.debug("failed OAuth lock cleanup", exc_info=True)
            logger.warning(
                "oauth refresh advisory lock unavailable provider=%s identity_fp=%s",
                provider,
                identity[:12],
                exc_info=True,
            )
            _count_unavailable(provider)
            async with self._local_lock(key):
                yield
            return

        # Do not catch exceptions from the guarded body: a failed refresh must
        # reach cooldown classification unchanged.
        try:
            yield
        finally:
            await lock_cm.__aexit__(None, None, None)

    @staticmethod
    @asynccontextmanager
    async def _local_lock(key: int) -> AsyncIterator[None]:
        with _locks_guard:
            lock = _locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield


__all__ = ["RefreshLock", "bind_metrics_registry"]
