"""Exact-match response cache.

Caches the JSON body returned by `POST /v1/chat/completions` (and the
non-streaming equivalent of streaming responses) keyed by a SHA-256 digest of
the request shape.

This is the Release-1 capability promised in `docs/feature-matrix-and-plan.md`.
Semantic caching arrives in Release 3 and is intentionally out of scope here.

Key composition (all parts canonicalised, then concatenated and SHA-256'd):
    pool_name | model | canonical(messages) | canonical(tools) | extra_body_hash

Storage:
    SQLite at the configured path (default `cache/cache.db`).
    Schema is intentionally minimal — a single `entries` table keyed by hash
    with `expires_at` and a JSON `body` blob. We don't try to be clever with
    partial responses; the whole assistant message (or the assembled
    streaming payload) is cached as one record.

Eviction:
    Lazy: `get()` rejects expired rows and deletes them on access.
    Bounded size: when inserting, if the entry count exceeds
    `max_entries`, the oldest non-pinned rows are deleted to make room.

Bypass:
    `X-Tusker-Cache: bypass` header skips the lookup but still records the
    new entry on write. This lets callers force-fresh responses without
    disabling the cache globally.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def canonical_json(obj: Any) -> str:
    """Produce a canonical JSON string for hashing.

    Sorts object keys recursively so that message ordering, key ordering,
    and trivial whitespace differences don't produce different cache keys.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_part(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def make_cache_key(
    *,
    pool_name: str | None,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    extra_body: dict[str, Any] | None,
) -> str:
    """Compute the deterministic cache key for a chat-completion request."""
    parts = [
        f"pool={pool_name or ''}",
        f"model={model or ''}",
        _hash_part(canonical_json(messages)),
        _hash_part(canonical_json(tools or [])),
        _hash_part(canonical_json(extra_body or {})),
    ]
    return _hash_part("\n".join(parts))


@dataclass
class CacheConfig:
    enabled: bool = False
    path: str = "cache/cache.db"
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
    """SQLite-backed exact-match cache for chat-completion responses."""

    def __init__(self, config: CacheConfig):
        self._config = config
        self.stats = CacheStats()
        if not config.enabled:
            return
        Path(config.path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_db()

    # -- schema ---------------------------------------------------------

    def _ensure_db(self) -> None:
        with sqlite3.connect(self._config.path) as conn:
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
        if not self._config.enabled:
            return None
        now = time.time()
        with sqlite3.connect(self._config.path) as conn:
            row = conn.execute(
                "SELECT body, expires_at FROM entries WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            self.stats.misses += 1
            return None
        body_json, expires_at = row
        if expires_at <= now:
            # Lazy eviction
            with sqlite3.connect(self._config.path) as conn:
                conn.execute("DELETE FROM entries WHERE key = ?", (key,))
                conn.commit()
            self.stats.misses += 1
            self.stats.evictions += 1
            return None
        # Bump hit counter (best-effort)
        with sqlite3.connect(self._config.path) as conn:
            conn.execute(
                "UPDATE entries SET hits = hits + 1 WHERE key = ?", (key,)
            )
            conn.commit()
        self.stats.hits += 1
        try:
            return json.loads(body_json)
        except json.JSONDecodeError:
            return None

    def put(self, key: str, body: dict[str, Any]) -> None:
        """Store a response body with the configured TTL."""
        if not self._config.enabled:
            return
        now = time.time()
        expires_at = now + self._config.ttl_secs
        with sqlite3.connect(self._config.path) as conn:
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

    def invalidate(self, key: str) -> None:
        """Remove a single entry (e.g. on tool-call response)."""
        if not self._config.enabled:
            return
        with sqlite3.connect(self._config.path) as conn:
            conn.execute("DELETE FROM entries WHERE key = ?", (key,))
            conn.commit()

    def stats_snapshot(self) -> dict[str, int]:
        return self.stats.snapshot()

    # -- size cap --------------------------------------------------------

    def _enforce_size_cap(self) -> None:
        cap = self._config.max_entries
        if cap <= 0:
            return
        with sqlite3.connect(self._config.path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            if count <= cap:
                return
            to_delete = count - cap
            # Delete the oldest non-pinned rows first.
            rows = conn.execute(
                """
                DELETE FROM entries
                WHERE key IN (
                    SELECT key FROM entries ORDER BY created_at ASC LIMIT ?
                )
                """,
                (to_delete,),
            ).rowcount
            conn.commit()
            self.stats.evictions += max(0, rows)


def load_cache_config_from_env(env: dict[str, str] | None = None) -> CacheConfig:
    """Build a CacheConfig from the environment (or a dict for tests)."""
    env = env if env is not None else __import__("os").environ
    enabled_raw = env.get("TUSKER_CACHE_ENABLED", "false").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    return CacheConfig(
        enabled=enabled,
        path=env.get("TUSKER_CACHE_PATH", "cache/cache.db"),
        ttl_secs=int(env.get("TUSKER_CACHE_TTL_SECS", "300")),
        max_entries=int(env.get("TUSKER_CACHE_MAX_ENTRIES", "10000")),
    )


__all__ = [
    "CacheConfig",
    "CacheStats",
    "ResponseCache",
    "canonical_json",
    "load_cache_config_from_env",
    "make_cache_key",
]
