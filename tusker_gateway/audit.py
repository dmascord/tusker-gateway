"""Append-only, integrity-chained audit events for gateway requests.

The default backend appends to the shared state database (PostgreSQL in
production, SQLite for local development and tests). Each append locks a
singleton chain-state row, so ordering and the hash chain stay correct across
processes without any NFS advisory locking. The historical JSONL file backend
is retained for migration and offline tooling; both backends emit the same
record shape and honour the same fail-closed contract.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import time
from typing import Any, Mapping

from aiohttp import web

from tusker_gateway.observability import get_access_log_context
from tusker_gateway.storage import StorageUnavailableError, shared_database

logger = logging.getLogger(__name__)

_GENESIS_HASH = "0" * 64
_MAX_AUDIT_TEXT_CHARS = 2_048
_MAX_AUDIT_COLLECTION_ITEMS = 32
_MAX_AUDIT_RECORD_BYTES = 64 * 1024
_MAX_LEGACY_RECORD_BYTES = 1024 * 1024
# A shared (NFS/RWX) audit volume could transiently reject an append while
# another writer's advisory lock was reaped. Bounded retry keeps the
# fail-closed contract from turning that into a client-visible 503.
_APPEND_RETRY_ATTEMPTS = 4
_APPEND_RETRY_BACKOFF_SECS = 0.02
# Tool-approval interception can call ``write_sync`` from inside a synchronous
# response validator, where the async retry loop is unreachable; the same
# shared-volume EIO would otherwise fail the request closed on the first
# transient blip.
_SYNC_APPEND_RETRY_ATTEMPTS = 4
_SYNC_APPEND_RETRY_BACKOFF_SECS = 0.02
_PRUNE_INTERVAL_SECS = 300.0

_AUDIT_TABLE = "audit_events"
_CHAIN_TABLE = "audit_chain_state"

try:  # pragma: no cover - Linux in production; fallback keeps local portability.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True)
class AuditConfig:
    path: str = ""
    hmac_key: str = ""
    fail_closed: bool = False
    fsync: bool = True
    exclude_paths: tuple[str, ...] = ("/health", "/ready", "/metrics")
    # "database" (production default from env) or "file" (legacy/offline).
    backend: str = "file"
    # Logical storage target. In postgres mode this resolves to the shared DSN;
    # in sqlite mode it is a local file (":memory:" for tests).
    database_path: str = ""
    retention_days: int = 90

    @property
    def enabled(self) -> bool:
        return self.backend == "database" or bool(self.path)

    @property
    def database_enabled(self) -> bool:
        return self.backend == "database"


def _bool_env(value: str, *, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _default_database_path() -> str:
    home = Path("/home/tusker/.hermes")
    if home.exists():
        return str(home / "audit.db")
    return "/tmp/tusker-audit.db"


def load_audit_config_from_env(env: Mapping[str, str] | None = None) -> AuditConfig:
    env = os.environ if env is None else env
    excluded = tuple(
        item.strip()
        for item in env.get("TUSKER_AUDIT_EXCLUDE_PATHS", "/health,/ready,/metrics").split(",")
        if item.strip()
    )
    backend = str(env.get("TUSKER_AUDIT_BACKEND", "database") or "database").strip().lower()
    if backend not in {"file", "database"}:
        logger.warning("unknown audit backend %r; using database", backend)
        backend = "database"
    try:
        retention_days = max(1, int(str(env.get("TUSKER_AUDIT_RETENTION_DAYS", "90"))))
    except (TypeError, ValueError):
        retention_days = 90
    return AuditConfig(
        path=env.get("TUSKER_AUDIT_LOG_PATH", "").strip(),
        hmac_key=env.get("TUSKER_AUDIT_HMAC_KEY", ""),
        fail_closed=_bool_env(env.get("TUSKER_AUDIT_FAIL_CLOSED", ""), default=False),
        fsync=_bool_env(env.get("TUSKER_AUDIT_FSYNC", ""), default=True),
        exclude_paths=excluded,
        backend=backend,
        database_path=str(env.get("TUSKER_AUDIT_DATABASE_PATH", "") or "").strip()
        or _default_database_path(),
        retention_days=retention_days,
    )


class AuditWriteError(RuntimeError):
    """Raised when fail-closed audit persistence cannot complete."""


class AuditLogger:
    """Write integrity-chained audit events to the state DB or a JSONL file."""

    def __init__(self, config: AuditConfig):
        self.config = config
        # Serialize file appends inside this process. ``flock`` still guards
        # cross-process writers, but on an RWX/NFS volume a burst of concurrent
        # requests otherwise piles up in ``to_thread`` workers where a
        # transient NFS error surfaces as a client-visible 503.
        self._write_lock = asyncio.Lock()
        self._db: Any = None
        self._last_prune = 0.0
        if config.database_enabled:
            policy = "critical" if config.fail_closed else "advisory"
            self._db = shared_database(
                config.database_path or _default_database_path(),
                timeout=10.0,
                fallback_policy=policy,
            )
            try:
                self._ensure_db()
            except Exception as exc:  # pragma: no cover - startup resilience
                # Do not latch the failure: the pool reconnects per connection,
                # so a later write may succeed once the database recovers.
                logger.warning("audit database schema check failed: %s", exc.__class__.__name__)

    # ------------------------------------------------------------------
    # Shared chain helpers
    # ------------------------------------------------------------------

    def _digest(self, previous_hash: str, canonical_event: bytes) -> str:
        message = previous_hash.encode("ascii") + b"." + canonical_event
        if self.config.hmac_key:
            return hmac.new(
                self.config.hmac_key.encode("utf-8"), message, hashlib.sha256
            ).hexdigest()
        return hashlib.sha256(message).hexdigest()

    @classmethod
    def _bounded_value(cls, value: Any, *, depth: int = 0) -> Any:
        """Bound caller-derived metadata so one event cannot poison the chain."""
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) <= _MAX_AUDIT_TEXT_CHARS:
                return value
            return value[: _MAX_AUDIT_TEXT_CHARS - 1] + "…"
        if depth >= 2:
            return cls._bounded_value(str(value), depth=depth)
        if isinstance(value, Mapping):
            return {
                cls._bounded_value(str(key), depth=depth + 1): cls._bounded_value(
                    item, depth=depth + 1
                )
                for key, item in list(value.items())[:_MAX_AUDIT_COLLECTION_ITEMS]
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [
                cls._bounded_value(item, depth=depth + 1)
                for item in list(value)[:_MAX_AUDIT_COLLECTION_ITEMS]
            ]
        return cls._bounded_value(str(value), depth=depth)

    def _canonical_payload(self, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            str(key)[:128]: self._bounded_value(value)
            for key, value in list(event.items())[:_MAX_AUDIT_COLLECTION_ITEMS]
        }
        payload.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        payload.setdefault(
            "policy_version",
            os.environ.get("TUSKER_POLICY_VERSION")
            or os.environ.get("TUSKER_COMMIT")
            or "unknown",
        )
        payload["chain_version"] = 2 if self.config.hmac_key else 1
        payload["chain_mode"] = "hmac" if self.config.hmac_key else "sha256"
        return payload

    def _canonical_bytes(self, payload: Mapping[str, Any]) -> bytes:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    # ------------------------------------------------------------------
    # File backend (legacy / migration source)
    # ------------------------------------------------------------------

    @staticmethod
    def _previous_hash(handle) -> str:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return _GENESIS_HASH
        # New records are bounded below 64 KiB, but scan farther so a log
        # created by an older release with one oversized final record remains
        # recoverable after upgrade.
        buffer = b""
        position = size
        while position > 0:
            start = max(0, position - 65_536)
            handle.seek(start)
            buffer = handle.read(position - start) + buffer
            position = start
            lines = [line for line in buffer.splitlines() if line.strip()]
            if len(buffer) > _MAX_LEGACY_RECORD_BYTES:
                raise AuditWriteError("audit log final record exceeds the recovery limit")
            if position == 0 or len(lines) >= 2:
                break
        if not lines:
            return _GENESIS_HASH
        record = json.loads(lines[-1].decode("utf-8"))
        candidate = record.get("hash")
        if not isinstance(candidate, str) or len(candidate) != 64:
            raise AuditWriteError("audit log contains an invalid chain record")
        return candidate

    def _append_file(self, event: Mapping[str, Any]) -> dict[str, Any]:
        path = Path(self.config.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                previous_hash = self._previous_hash(handle)
                payload = self._canonical_payload(event)
                payload["previous_hash"] = previous_hash
                canonical = self._canonical_bytes(payload)
                if len(canonical) > _MAX_AUDIT_RECORD_BYTES:
                    raise AuditWriteError("audit event exceeds the maximum record size")
                payload["hash"] = self._digest(previous_hash, canonical)
                serialized = json.dumps(
                    payload, sort_keys=True, ensure_ascii=False
                ).encode("utf-8")
                if len(serialized) > _MAX_AUDIT_RECORD_BYTES:
                    raise AuditWriteError("audit event exceeds the maximum record size")
                handle.seek(0, os.SEEK_END)
                handle.write(serialized + b"\n")
                handle.flush()
                if self.config.fsync:
                    os.fsync(handle.fileno())
                return payload
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # Database backend
    # ------------------------------------------------------------------

    def _ensure_db(self) -> None:
        assert self._db is not None
        with self._db.connection() as conn:
            if conn.degraded:
                return
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_CHAIN_TABLE} (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    head_hash TEXT NOT NULL,
                    head_sequence BIGINT NOT NULL,
                    tail_sequence BIGINT,
                    tail_hash TEXT
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_AUDIT_TABLE} (
                    sequence BIGINT PRIMARY KEY,
                    event_id TEXT UNIQUE,
                    event_type TEXT,
                    request_id TEXT,
                    timestamp TEXT,
                    chain_version INTEGER NOT NULL,
                    chain_mode TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_audit_events_request "
                f"ON {_AUDIT_TABLE} (request_id)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_audit_events_created "
                f"ON {_AUDIT_TABLE} (created_at)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_audit_events_type "
                f"ON {_AUDIT_TABLE} (event_type)"
            )
            cursor = conn.execute(
                f"SELECT head_hash FROM {_CHAIN_TABLE} WHERE id = 1"
            )
            if cursor.fetchone() is None:
                conn.execute(
                    f"INSERT INTO {_CHAIN_TABLE} (id, head_hash, head_sequence) "
                    f"VALUES (1, ?, 0)",
                    (_GENESIS_HASH,),
                )

    def _append_db(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        assert self._db is not None
        with self._db.connection() as conn:
            if conn.degraded:
                # Advisory mode: a state-store outage must not fail the caller.
                return None
            # Lock the singleton row so concurrent writers cannot observe the
            # same head. PostgreSQL honours FOR UPDATE; SQLite serializes the
            # whole short transaction via BEGIN IMMEDIATE.
            lock = " FOR UPDATE" if conn.is_postgres else ""
            cursor = conn.execute(
                f"SELECT head_hash, head_sequence FROM {_CHAIN_TABLE} "
                f"WHERE id = 1{lock}"
            )
            row = cursor.fetchone()
            if row is None:
                raise OSError("audit chain state is missing")
            previous_hash = str(row[0])
            sequence = int(row[1]) + 1

            payload = self._canonical_payload(event)
            payload["previous_hash"] = previous_hash
            canonical = self._canonical_bytes(payload)
            if len(canonical) > _MAX_AUDIT_RECORD_BYTES:
                raise AuditWriteError("audit event exceeds the maximum record size")
            digest = self._digest(previous_hash, canonical)
            payload["hash"] = digest
            serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            if len(serialized.encode("utf-8")) > _MAX_AUDIT_RECORD_BYTES:
                raise AuditWriteError("audit event exceeds the maximum record size")

            conn.execute(
                f"INSERT INTO {_AUDIT_TABLE} "
                f"(sequence, event_id, event_type, request_id, timestamp, "
                f" chain_version, chain_mode, previous_hash, hash, payload, created_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sequence,
                    payload.get("event_id"),
                    payload.get("event_type"),
                    payload.get("request_id"),
                    payload.get("timestamp"),
                    int(payload["chain_version"]),
                    str(payload["chain_mode"]),
                    previous_hash,
                    digest,
                    serialized,
                    time.time(),
                ),
            )
            conn.execute(
                f"UPDATE {_CHAIN_TABLE} SET head_hash = ?, head_sequence = ? "
                f"WHERE id = 1",
                (digest, sequence),
            )
        result = dict(payload)
        result["sequence"] = sequence
        return result

    def prune(self) -> int:
        """Delete events older than the retention window, preserving the chain anchor."""
        if self._db is None:
            return 0
        cutoff = time.time() - self.config.retention_days * 86_400
        with self._db.connection() as conn:
            if conn.degraded:
                return 0
            row = conn.execute(
                f"SELECT MIN(sequence) FROM {_AUDIT_TABLE} WHERE created_at >= ?",
                (cutoff,),
            ).fetchone()
            keep_from = int(row[0]) if row and row[0] is not None else None
            if keep_from is None:
                return 0
            anchor = conn.execute(
                f"SELECT hash FROM {_AUDIT_TABLE} WHERE sequence < ? "
                f"ORDER BY sequence DESC LIMIT 1",
                (keep_from,),
            ).fetchone()
            anchor_hash = str(anchor[0]) if anchor else _GENESIS_HASH
            cursor = conn.execute(
                f"DELETE FROM {_AUDIT_TABLE} WHERE sequence < ?", (keep_from,)
            )
            deleted = int(getattr(cursor, "rowcount", 0) or 0)
            # tail_sequence is the last *pruned* sequence so verify starts at
            # tail_sequence + 1, i.e. the first retained row.
            conn.execute(
                f"UPDATE {_CHAIN_TABLE} SET tail_sequence = ?, tail_hash = ? WHERE id = 1",
                (keep_from - 1, anchor_hash),
            )
            return deleted

    @classmethod
    def verify_db(
        cls,
        config: AuditConfig,
        *,
        database_path: str = "",
    ) -> tuple[bool, int]:
        """Verify every hash link in the database chain; return (valid, count)."""
        db = shared_database(
            database_path or config.database_path or _default_database_path(),
            timeout=10.0,
            fallback_policy="critical",
        )
        verifier = cls(config)
        previous = _GENESIS_HASH
        count = 0
        with db.connection() as conn:
            if conn.degraded:
                return False, 0
            row = conn.execute(
                f"SELECT tail_hash, tail_sequence FROM {_CHAIN_TABLE} WHERE id = 1"
            ).fetchone()
            start_sequence = 0
            if row is not None and row[0]:
                previous = str(row[0])
                start_sequence = int(row[1]) + 1
            cursor = conn.execute(
                f"SELECT sequence, previous_hash, hash, payload FROM {_AUDIT_TABLE} "
                f"WHERE sequence >= ? ORDER BY sequence",
                (start_sequence,),
            )
            for _sequence, claimed_previous, claimed_hash, payload_text in cursor:
                if str(claimed_previous) != previous:
                    return False, count
                record = json.loads(str(payload_text))
                record.pop("hash", None)
                canonical = json.dumps(
                    record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
                mode = record.get("chain_mode", "sha256")
                if mode == "hmac":
                    digest = verifier._digest(previous, canonical)
                else:
                    digest = hashlib.sha256(
                        previous.encode("ascii") + b"." + canonical
                    ).hexdigest()
                if not hmac.compare_digest(str(claimed_hash), digest):
                    return False, count
                previous = str(claimed_hash)
                count += 1
        return True, count

    # ------------------------------------------------------------------
    # Common write API
    # ------------------------------------------------------------------

    def _append(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        if self._db is not None:
            return self._append_db(event)
        return self._append_file(event)

    async def write(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        if not self.config.enabled:
            return None
        last_error: BaseException | None = None
        for attempt in range(_APPEND_RETRY_ATTEMPTS):
            try:
                async with self._write_lock:
                    return await asyncio.to_thread(self._append, event)
            except (OSError, StorageUnavailableError) as exc:
                # A shared/NFS-backed audit volume can reject an append while
                # another writer's lock is being reaped; a state-store
                # connection can also fail transiently. Retry with backoff
                # instead of failing the caller closed.
                last_error = exc
                logger.warning(
                    "audit append retry %d/%d after %s",
                    attempt + 1,
                    _APPEND_RETRY_ATTEMPTS,
                    exc.__class__.__name__,
                )
                if attempt + 1 < _APPEND_RETRY_ATTEMPTS:
                    await asyncio.sleep(_APPEND_RETRY_BACKOFF_SECS * (2**attempt))
            except Exception as exc:
                logger.error("audit write failed: %s", exc.__class__.__name__)
                if self.config.fail_closed:
                    raise AuditWriteError("required audit persistence failed") from exc
                return None
        logger.error(
            "audit write exhausted retries: %s",
            last_error.__class__.__name__ if last_error else "unknown",
        )
        if self.config.fail_closed and last_error is not None:
            raise AuditWriteError("required audit persistence failed") from last_error
        return None

    def write_sync(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        """Synchronously append a non-request audit event.

        Tool approval interception can occur inside a synchronous response
        validator, including while an async stream is being assembled.  This
        adapter preserves the same chained format and failure policy without
        scheduling a task that could outlive the request.
        """
        if not self.config.enabled:
            return None
        last_error: BaseException | None = None
        for attempt in range(_SYNC_APPEND_RETRY_ATTEMPTS):
            try:
                return self._append(event)
            except (OSError, StorageUnavailableError) as exc:
                last_error = exc
                logger.warning(
                    "audit sync append retry %d/%d after %s",
                    attempt + 1,
                    _SYNC_APPEND_RETRY_ATTEMPTS,
                    exc.__class__.__name__,
                )
                if attempt + 1 < _SYNC_APPEND_RETRY_ATTEMPTS:
                    time.sleep(_SYNC_APPEND_RETRY_BACKOFF_SECS * (2**attempt))
        logger.error("audit write failed: %s", last_error.__class__.__name__)
        if self.config.fail_closed and last_error is not None:
            raise AuditWriteError("required audit persistence failed") from last_error
        return None

    # ------------------------------------------------------------------
    # Verification / tooling
    # ------------------------------------------------------------------

    @classmethod
    def verify_file(cls, config: AuditConfig) -> tuple[bool, int]:
        """Verify every hash link in an audit file; return (valid, count)."""
        if not config.path or not Path(config.path).exists():
            return True, 0
        verifier = cls(config)
        previous = _GENESIS_HASH
        count = 0
        try:
            with Path(config.path).open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    claimed = record.pop("hash")
                    if record.get("previous_hash") != previous:
                        return False, count
                    canonical = json.dumps(
                        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode("utf-8")
                    mode = record.get("chain_mode", "sha256")
                    if mode not in {"sha256", "hmac"}:
                        return False, count
                    digest = verifier._digest(previous, canonical) if mode == "hmac" else hashlib.sha256(previous.encode("ascii") + b"." + canonical).hexdigest()
                    if not hmac.compare_digest(claimed, digest):
                        return False, count
                    previous = claimed
                    count += 1
        except (OSError, ValueError, KeyError, TypeError):
            return False, count
        return True, count


def _base_event(request: web.Request, status: int, started: float) -> dict[str, Any]:
    identity = request.get("identity")
    event: dict[str, Any] = {
        "event_id": f"evt_{secrets.token_hex(12)}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": "gateway.request",
        "request_id": request.get("_request_id", "unknown"),
        "method": request.method,
        "path": request.path,
        "status": status,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "outcome": "allowed" if status < 400 else "denied" if status in {401, 403} else "failed",
    }
    if identity is not None:
        event.update(
            {
                "principal": getattr(identity, "principal", "unknown"),
                "tenant": getattr(identity, "tenant", "unknown"),
                "key_fingerprint": getattr(identity, "key_fingerprint", "unknown"),
            }
        )
    event.update(get_access_log_context(request))
    if request.get("_deadline_exceeded"):
        event["reason"] = "request_timeout"
    if request.get("_stream_error"):
        event["stream_error"] = request["_stream_error"]
    if request.get("_stream_error_code"):
        event["stream_error_code"] = request["_stream_error_code"]
    if request.get("_stream_error_detail"):
        event["stream_error_detail"] = request["_stream_error_detail"]
    if request.get("_idempotency_replayed"):
        event["idempotency_replayed"] = True
    return event


def attach_audit_middleware(app: web.Application, audit: AuditLogger) -> None:
    if not audit.config.enabled:
        return

    @web.middleware
    async def audit_middleware(request: web.Request, handler):
        if request.path in audit.config.exclude_paths:
            return await handler(request)
        started = time.monotonic()
        response: web.StreamResponse | None = None
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            await audit.write(_base_event(request, exc.status, started))
            raise
        except Exception:
            status = 504 if request.get("_deadline_exceeded") else 500
            await audit.write(_base_event(request, status, started))
            raise
        try:
            await audit.write(_base_event(request, response.status, started))
        except AuditWriteError:
            if response.prepared:
                logger.critical("audit failed after response preparation; closing transport")
                if request.transport is not None:
                    request.transport.close()
                return response
            return web.json_response(
                {
                    "error": {
                        "type": "server_error",
                        "message": "Required audit persistence is unavailable",
                        "code": "audit_unavailable",
                    }
                },
                status=503,
            )
        return response

    app.middlewares.append(audit_middleware)


__all__ = [
    "AuditConfig",
    "AuditLogger",
    "AuditWriteError",
    "attach_audit_middleware",
    "load_audit_config_from_env",
]
