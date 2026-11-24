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
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

logger = logging.getLogger(__name__)

_LOCK_NAMESPACE = 0x5455534B  # "TUSK" - keeps keys disjoint from other apps
_locks: dict[int, asyncio.Lock] = {}
_locks_guard = threading.Lock()


def _lock_key(provider: str, identity: str) -> int:
    """Stable 64-bit advisory-lock key for a credential identity."""
    digest = hashlib.sha256(f"{provider}:{identity}".encode("utf-8")).digest()
    return _LOCK_NAMESPACE << 32 | int.from_bytes(digest[:4], "big")


class RefreshLock:
    """Wrap a DB handle with a cross-process credential-refresh lock."""

    def __init__(self, *, acquire: Callable[[str], Any] | None = None) -> None:
        # ``acquire`` is an optional callback returning a context manager that
        # owns an advisory lock for a string key. It is supplied by the DB
        # store so this module does not import config_store (which would
        # create a cycle through config_runtime).
        self._acquire = acquire

    @asynccontextmanager
    async def guard(self, provider: str, identity: str) -> AsyncIterator[None]:
        key = _lock_key(provider, identity)
        if self._acquire is None:
            async with self._local_lock(key):
                yield
            return

        try:
            async with self._acquire(key):
                yield
        except Exception:  # noqa: BLE001 - fail open, never block auth
            logger.warning(
                "oauth refresh advisory lock unavailable provider=%s identity_fp=%s",
                provider,
                identity[:12],
                exc_info=True,
            )
            async with self._local_lock(key):
                yield

    @staticmethod
    @asynccontextmanager
    async def _local_lock(key: int) -> AsyncIterator[None]:
        with _locks_guard:
            lock = _locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield


def load_refresh_lock_from_env() -> bool:
    """Whether cross-process refresh locking is enabled (default on)."""
    raw = os.environ.get("TUSKER_OAUTH_REFRESH_LOCK", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


__all__ = ["RefreshLock", "load_refresh_lock_from_env"]
