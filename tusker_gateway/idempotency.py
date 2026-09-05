"""Persistent idempotency-key handling for non-streaming API requests."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from aiohttp import web

from tusker_gateway.errors import openai_error
from tusker_gateway.identity import extract_api_key, fingerprint_api_key
from tusker_gateway.observability import set_access_log_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdempotencyConfig:
    enabled: bool = False
    path: str = "/tmp/tusker-idempotency.db"
    ttl_secs: int = 86_400
    lock_secs: int = 300
    max_response_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True)
class Claim:
    state: str
    status: int | None = None
    body: bytes | None = None
    content_type: str | None = None


def _default_path() -> str:
    home = Path("/home/tusker/.hermes")
    if home.exists():
        return str(home / "idempotency.db")
    return "/tmp/tusker-idempotency.db"


def _int_env(value: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def load_idempotency_config_from_env(
    env: Mapping[str, str] | None = None,
) -> IdempotencyConfig:
    env = os.environ if env is None else env
    enabled = env.get("TUSKER_IDEMPOTENCY_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    return IdempotencyConfig(
        enabled=enabled,
        path=env.get("TUSKER_IDEMPOTENCY_PATH", "").strip() or _default_path(),
        ttl_secs=_int_env(env.get("TUSKER_IDEMPOTENCY_TTL_SECS", "86400"), 86_400),
        lock_secs=_int_env(env.get("TUSKER_IDEMPOTENCY_LOCK_SECS", "300"), 300),
        max_response_bytes=_int_env(
            env.get("TUSKER_IDEMPOTENCY_MAX_RESPONSE_BYTES", "2097152"),
            2 * 1024 * 1024,
        ),
    )


class IdempotencyStore:
    """SQLite-backed reservation and replay store, safe across processes."""

    def __init__(self, config: IdempotencyConfig):
        self.config = config
        if config.enabled:
            path = Path(config.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.path, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _ensure_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    record_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    response_status INTEGER,
                    response_body BLOB,
                    content_type TEXT,
                    locked_until REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_idempotency_expires
                ON idempotency_records (expires_at)
                """
            )

    def claim(self, record_key: str, request_hash: str) -> Claim:
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM idempotency_records WHERE expires_at <= ?", (now,))
            row = conn.execute(
                """
                SELECT request_hash, state, response_status, response_body,
                       content_type, locked_until
                FROM idempotency_records WHERE record_key = ?
                """,
                (record_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO idempotency_records
                    (record_key, request_hash, state, locked_until, expires_at,
                     created_at, updated_at)
                    VALUES (?, ?, 'processing', ?, ?, ?, ?)
                    """,
                    (
                        record_key,
                        request_hash,
                        now + self.config.lock_secs,
                        now + self.config.ttl_secs,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return Claim("claimed")

            stored_hash, state, status, body, content_type, locked_until = row
            if stored_hash != request_hash:
                conn.commit()
                return Claim("conflict")
            if state == "complete":
                conn.commit()
                return Claim(
                    "replay",
                    status=int(status),
                    body=bytes(body or b""),
                    content_type=content_type,
                )
            if float(locked_until) > now:
                conn.commit()
                return Claim("in_progress")

            conn.execute(
                """
                UPDATE idempotency_records
                SET locked_until = ?, expires_at = ?, updated_at = ?
                WHERE record_key = ?
                """,
                (now + self.config.lock_secs, now + self.config.ttl_secs, now, record_key),
            )
            conn.commit()
            return Claim("claimed")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete(
        self,
        record_key: str,
        request_hash: str,
        status: int,
        body: bytes,
        content_type: str | None,
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE idempotency_records
                SET state = 'complete', response_status = ?, response_body = ?,
                    content_type = ?, locked_until = 0, updated_at = ?
                WHERE record_key = ? AND request_hash = ? AND state = 'processing'
                """,
                (status, body, content_type, now, record_key, request_hash),
            )

    def abandon(self, record_key: str, request_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM idempotency_records
                WHERE record_key = ? AND request_hash = ? AND state = 'processing'
                """,
                (record_key, request_hash),
            )


def _canonical_request_hash(request: web.Request, body: bytes) -> str:
    canonical = body
    if body:
        try:
            parsed = json.loads(body)
            canonical = json.dumps(
                parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            pass
    digest = hashlib.sha256()
    digest.update(request.method.encode("ascii"))
    digest.update(b"\0")
    digest.update(request.path.encode("utf-8"))
    digest.update(b"\0")
    query = [
        (key, list(request.query.getall(key)))
        for key in sorted(set(request.query))
    ]
    digest.update(
        json.dumps(query, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    digest.update(b"\0")
    digest.update(canonical)
    return digest.hexdigest()


def _record_key(request: web.Request, idempotency_key: str) -> str:
    fingerprint = request.get("_api_key_fingerprint")
    if not fingerprint:
        api_key = extract_api_key(request)
        fingerprint = fingerprint_api_key(api_key) if api_key else "anonymous"
    scope = f"{fingerprint}\0{request.method}\0{request.path}\0{idempotency_key}"
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()


async def _abandon_safely(
    store: IdempotencyStore,
    record_key: str,
    request_hash: str,
) -> None:
    """Release a reservation without replacing the request's real failure."""
    try:
        await asyncio.shield(
            asyncio.to_thread(store.abandon, record_key, request_hash)
        )
    except (OSError, sqlite3.Error):
        logger.exception("could not release idempotency reservation")


def attach_idempotency_middleware(
    app: web.Application, store: IdempotencyStore
) -> None:
    if not store.config.enabled:
        return

    @web.middleware
    async def idempotency_middleware(request: web.Request, handler):
        if request.method != "POST" or not request.path.startswith("/v1/"):
            return await handler(request)
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            return await handler(request)
        if len(key) > 128 or any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
            return web.json_response(
                openai_error(
                    "Idempotency-Key must contain 1-128 printable non-space characters",
                    code="invalid_idempotency_key",
                    error_type="invalid_request_error",
                ),
                status=400,
            )

        # aiohttp's multipart parser consumes request.content directly. Reading
        # it here would leave image edit/variation handlers with an empty body.
        # Idempotency is therefore explicit for JSON request contracts only.
        if request.content_type != "application/json" and not request.content_type.endswith(
            "+json"
        ):
            return await handler(request)

        body = await request.read()
        try:
            parsed = json.loads(body) if body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("stream") is True:
            return await handler(request)

        record_key = _record_key(request, key)
        request_hash = _canonical_request_hash(request, body)
        try:
            claim = await asyncio.to_thread(store.claim, record_key, request_hash)
        except (OSError, sqlite3.Error):
            return web.json_response(
                openai_error(
                    "Idempotency persistence is unavailable",
                    code="idempotency_unavailable",
                    error_type="server_error",
                ),
                status=503,
            )
        if claim.state == "conflict":
            return web.json_response(
                openai_error(
                    "Idempotency-Key was already used with a different request",
                    code="idempotency_conflict",
                    error_type="invalid_request_error",
                ),
                status=409,
            )
        if claim.state == "in_progress":
            return web.json_response(
                openai_error(
                    "A request with this Idempotency-Key is still in progress",
                    code="idempotency_in_progress",
                    error_type="invalid_request_error",
                ),
                status=409,
                headers={"Retry-After": "1"},
            )
        if claim.state == "replay":
            request["_idempotency_replayed"] = True
            set_access_log_context(request, cache_status="idempotency_hit")
            headers = {
                "Idempotency-Replayed": "true",
                "Idempotency-Key": key,
            }
            if claim.content_type:
                headers["Content-Type"] = claim.content_type
            return web.Response(
                status=claim.status or 200,
                body=claim.body or b"",
                headers=headers,
            )

        try:
            set_access_log_context(request, cache_status="idempotency_miss")
            response = await handler(request)
        except asyncio.CancelledError:
            await _abandon_safely(store, record_key, request_hash)
            raise
        except Exception:
            await _abandon_safely(store, record_key, request_hash)
            raise

        response_body = response.body if isinstance(response, web.Response) else None
        if (
            response_body is not None
            and 200 <= response.status < 300
            and len(response_body) <= store.config.max_response_bytes
        ):
            body_bytes = bytes(response_body)
            try:
                await asyncio.to_thread(
                    store.complete,
                    record_key,
                    request_hash,
                    response.status,
                    body_bytes,
                    response.headers.get("Content-Type"),
                )
            except (OSError, sqlite3.Error):
                return web.json_response(
                    openai_error(
                        "The operation completed but its idempotency record could not be persisted",
                        code="idempotency_persist_failed",
                        error_type="server_error",
                    ),
                    status=503,
                    headers={"Idempotency-Key": key, "Retry-After": str(store.config.lock_secs)},
                )
            if not response.prepared:
                response.headers["Idempotency-Key"] = key
                response.headers["Idempotency-Replayed"] = "false"
        else:
            await _abandon_safely(store, record_key, request_hash)
        return response

    app.middlewares.append(idempotency_middleware)


__all__ = [
    "Claim",
    "IdempotencyConfig",
    "IdempotencyStore",
    "attach_idempotency_middleware",
    "load_idempotency_config_from_env",
]
