"""Exact-match response cache.

Caches the JSON body returned by `POST /v1/chat/completions` (and the
non-streaming equivalent of streaming responses) keyed by a SHA-256 digest of
the request shape.

Storage uses the active gateway state backend (PostgreSQL in postgres mode,
SQLite in sqlite mode).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tusker_gateway.storage import shared_database, storage_error_types

logger = logging.getLogger(__name__)


def _default_path() -> str:
    """Return the default cache DB path, preferring the persistent HOME volume."""
    home = os.environ.get("HOME", "")
    if home:
        return os.path.join(home, ".hermes", "cache.db")
    return "cache/cache.db"


def canonical_json(obj: Any) -> str:
    """Produce a canonical JSON string for hashing.

    Sorts object keys recursively so that message ordering, key ordering,
    and trivial whitespace differences don't produce different cache keys.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_part(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def make_caller_scope(api_key: str) -> str:
    """Return a stable, non-reversible cache namespace for an API key."""
    return _hash_part(api_key) if api_key else ""


def make_cache_key(
    *,
    pool_name: str | None,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    extra_body: dict[str, Any] | None,
    caller_scope: str | None = None,
    provider: str | None = None,
    target_model: str | None = None,
) -> str:
    """Compute the deterministic cache key for a chat-completion request.

    ``caller_scope`` should be a one-way API-key/tenant fingerprint.  It is
    optional for backwards-compatible callers, but the HTTP endpoint always
    supplies it so cached responses cannot cross caller boundaries.
    """
    parts = [
        f"caller={caller_scope or ''}",
        f"pool={pool_name or ''}",
        f"model={model or ''}",
        f"provider={provider or ''}",
        f"target_model={target_model or ''}",
        _hash_part(canonical_json(messages)),
        _hash_part(canonical_json(tools or [])),
        _hash_part(canonical_json(extra_body or {})),
    ]
    return _hash_part("\n".join(parts))


@dataclass
class CacheConfig:
    enabled: bool = False
    path: str = field(default_factory=_default_path)
    ttl_secs: int = 300
    max_entries: int = 10_000


@dataclass
class CacheStats:
    """In-memory counters; mirrored into Prometheus on scrape."""
    hits: int = 0
    misses: int = 0
    writes: int = 0
    evictions: int = 0
    bypasses: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "evictions": self.evictions,
            "bypasses": self.bypasses,
        }


class ResponseCache:
    """Exact-match cache for chat-completion responses."""

    def __init__(self, config: CacheConfig):
        self._config = config
        self.stats = CacheStats()
        self._db = None
        if not config.enabled:
            return
        try:
            self._db = shared_database(config.path, timeout=5.0, fallback_policy="advisory")
            if not self._db.is_postgres and config.path != ":memory:":
                Path(config.path).parent.mkdir(parents=True, exist_ok=True)
            self._ensure_db()
        except Exception as exc:
            # Read-only filesystem/backend unavailable: silently disable this instance.
            logger.warning("cache disabled: cannot initialise %s: %s", config.path, exc)
            self._db = None
            self._config.enabled = False

    # -- schema ---------------------------------------------------------

    def _ensure_db(self) -> None:
        assert self._db is not None
        with self._db.connection() as conn:
            if not self._db.is_postgres:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    key TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_expires ON entries(expires_at)"
            )
            conn.commit()

    # -- public API ------------------------------------------------------

    def get(self, key: str) -> dict[str, Any] | None:
        """Return a cached response body or None if missing/expired."""
        if not self._config.enabled or self._db is None:
            return None
        now = time.time()
        try:
            with self._db.connection() as conn:
                row = conn.execute(
                    "SELECT body, expires_at FROM entries WHERE key = ?", (key,)
                ).fetchone()
        except storage_error_types():
            self.stats.misses += 1
            return None
        if row is None:
            self.stats.misses += 1
            logger.debug("cache miss key=%s", key[:16])
            return None
        body_json, expires_at = row
        if float(expires_at) <= now:
            # Lazy eviction
            try:
                with self._db.connection() as conn:
                    conn.execute("DELETE FROM entries WHERE key = ?", (key,))
                    conn.commit()
            except storage_error_types():
                pass
            self.stats.misses += 1
            self.stats.evictions += 1
            return None
        # Bump hit counter (best-effort)
        try:
            with self._db.connection() as conn:
                conn.execute(
                    "UPDATE entries SET hits = hits + 1 WHERE key = ?", (key,)
                )
                conn.commit()
        except storage_error_types():
            pass
        self.stats.hits += 1
        logger.debug("cache hit key=%s", key[:16])
        try:
            return json.loads(body_json)
        except Exception:
            return None

    def put(self, key: str, body: dict[str, Any]) -> None:
        """Store a response body with the configured TTL."""
        if not self._config.enabled or self._db is None:
            return
        now = time.time()
        expires_at = now + self._config.ttl_secs
        try:
            with self._db.connection() as conn:
                # Idempotent: same key overwrites in place and refreshes TTL.
                conn.execute(
                    """
                    INSERT INTO entries (key, body, created_at, expires_at, hits)
                    VALUES (?, ?, ?, ?, 0)
                    ON CONFLICT(key) DO UPDATE SET
                        body = excluded.body,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at,
                        hits = 0
                    """,
                    (key, json.dumps(body, ensure_ascii=False), now, expires_at),
                )
                conn.commit()
            # Enforce size cap.
            self._enforce_size_cap()
            self.stats.writes += 1
            logger.debug("cache store key=%s", key[:16])
        except storage_error_types() as exc:
            logger.warning("cache write failed: %s", exc)

    def invalidate(self, key: str) -> None:
        """Remove a single entry (e.g. on tool-call response)."""
        if not self._config.enabled or self._db is None:
            return
        try:
            with self._db.connection() as conn:
                conn.execute("DELETE FROM entries WHERE key = ?", (key,))
                conn.commit()
        except storage_error_types():
            return
        logger.debug("cache invalidate key=%s", key[:16])

    def stats_snapshot(self) -> dict[str, int]:
        return self.stats.snapshot()

    # -- size cap --------------------------------------------------------

    def _enforce_size_cap(self) -> None:
        cap = self._config.max_entries
        if cap <= 0 or self._db is None:
            return
        try:
            with self._db.connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                if int(count) <= cap:
                    return
                to_delete = int(count) - cap
                # Delete the oldest rows first.
                conn.execute(
                    """
                    DELETE FROM entries
                    WHERE key IN (
                        SELECT key FROM entries ORDER BY created_at ASC LIMIT ?
                    )
                    """,
                    (to_delete,),
                )
                conn.commit()
                self.stats.evictions += max(0, to_delete)
        except storage_error_types():
            return


def load_cache_config_from_env(env: dict[str, str] | None = None) -> CacheConfig:
    """Build a CacheConfig from the environment (or a dict for tests)."""
    env = env if env is not None else __import__("os").environ
    enabled_raw = env.get("TUSKER_CACHE_ENABLED", "false").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    return CacheConfig(
        enabled=enabled,
        path=env.get("TUSKER_CACHE_PATH") or _default_path(),
        ttl_secs=int(env.get("TUSKER_CACHE_TTL_SECS", "300")),
        max_entries=int(env.get("TUSKER_CACHE_MAX_ENTRIES", "10000")),
    )


__all__ = [
    "CacheConfig",
    "CacheStats",
    "ResponseCache",
    "canonical_json",
    "load_cache_config_from_env",
    "make_caller_scope",
    "make_cache_key",
]


def load_cache_config_from_env(env: dict[str, str] | None = None) -> CacheConfig:
    """Build a CacheConfig from the environment (or a dict for tests)."""
    env = env if env is not None else __import__("os").environ
    enabled_raw = env.get("TUSKER_CACHE_ENABLED", "false").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    return CacheConfig(
        enabled=enabled,
        path=env.get("TUSKER_CACHE_PATH") or _default_path(),
        ttl_secs=int(env.get("TUSKER_CACHE_TTL_SECS", "300")),
        max_entries=int(env.get("TUSKER_CACHE_MAX_ENTRIES", "10000")),
    )


__all__ = [
    "CacheConfig",
    "CacheStats",
    "ResponseCache",
    "canonical_json",
    "load_cache_config_from_env",
    "make_caller_scope",
    "make_cache_key",
]
