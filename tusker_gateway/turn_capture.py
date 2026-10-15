"""Durable captures of rejected provider turns, replayable from the database.

A rejected tool turn is normally hidden from the client by an immediate
failover, so the provider's bytes and the request the gateway actually sent are
the only evidence that the verdict was correct. Those two payloads used to be
written to a bounded directory on the gateway's PVC, which meant an operator had
to clear it by hand and captures stopped silently once its budget filled.

This module keeps the same evidence in the shared state database instead, with a
TTL, so a rejection stays queryable and replayable (`tusker_gateway.tools
.replay_capture`) long after the container log has rotated away.

Retention policy (see ``docs/turn-capture-replay.md``):

* ``pool`` is recorded on every row.
* Prompt bodies are stored only for pools that are neither excluded by name nor
  unknown. The ``privacy`` pool is excluded by default because it is the ZDR
  boundary; a turn whose pool cannot be established keeps its verdict counters
  but no bytes, because the ZDR policy cannot be proven for it.
* Rows expire after ``TUSKER_TURN_CAPTURE_TTL_SECS`` (7 days by default).

Writes take the ``advisory`` fallback policy: capture is diagnostics, so a
database outage must degrade to "no capture" rather than fail a client request.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tusker_gateway.storage import shared_database

logger = logging.getLogger(__name__)

_TABLE = "turn_captures"
_PRUNE_INTERVAL_SECS = 300.0

# Body retention outcomes recorded on every row so an auditor can tell a
# complete capture from a deliberate omission without reading the payloads.
BODY_CAPTURED = "captured"
BODY_REQUEST_UNAVAILABLE = "request_unavailable"
BODY_EXCLUDED_POOL = "excluded_pool"
BODY_UNKNOWN_POOL = "unknown_pool"


@dataclass(frozen=True)
class TurnCaptureConfig:
    """Environment-derived capture policy."""

    enabled: bool = False
    path: str = "/tmp/tusker-turn-captures.db"
    ttl_secs: int = 604_800
    max_request_bytes: int = 4 * 1024 * 1024
    max_response_bytes: int = 256 * 1024
    pending_bytes: int = 16 * 1024 * 1024
    excluded_pools: tuple[str, ...] = ("privacy",)


def _default_path() -> str:
    home = Path("/home/tusker/.hermes")
    if home.exists():
        return str(home / "turn-captures.db")
    return "/tmp/tusker-turn-captures.db"


def _int_env(value: str | None, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(str(value)))
    except (TypeError, ValueError):
        return default


def load_turn_capture_config_from_env(
    env: Mapping[str, str] | None = None,
) -> TurnCaptureConfig:
    """Build the capture policy from the process environment.

    ``TUSKER_STREAM_CAPTURE`` is the enable switch and is shared with the
    historical directory capture so the deployment flag keeps its meaning.
    """
    values = os.environ if env is None else env
    enabled = str(values.get("TUSKER_STREAM_CAPTURE", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    pools = tuple(
        part.strip().lower()
        for part in str(values.get("TUSKER_TURN_CAPTURE_EXCLUDE_POOLS", "privacy")).split(",")
        if part.strip()
    )
    return TurnCaptureConfig(
        enabled=enabled,
        path=str(values.get("TUSKER_TURN_CAPTURE_PATH", "")).strip() or _default_path(),
        ttl_secs=_int_env(values.get("TUSKER_TURN_CAPTURE_TTL_SECS"), 604_800),
        max_request_bytes=_int_env(
            values.get("TUSKER_TURN_CAPTURE_MAX_REQUEST_BYTES"), 4 * 1024 * 1024
        ),
        max_response_bytes=_int_env(
            values.get("TUSKER_TURN_CAPTURE_MAX_RESPONSE_BYTES"), 256 * 1024
        ),
        pending_bytes=_int_env(
            values.get("TUSKER_TURN_CAPTURE_PENDING_BYTES"), 16 * 1024 * 1024
        ),
        excluded_pools=pools,
    )


def _excerpt(payload: bytes, limit: int) -> tuple[bytes, int]:
    """Return a storable body and the number of bytes omitted from its middle.

    A head+tail excerpt keeps the request preamble (model, tools, first turns)
    and the request tail, while ``truncated_bytes`` records that the middle is
    gone so replay can refuse instead of replaying a different request.
    """
    if len(payload) <= limit:
        return payload, 0
    head = limit // 2
    tail = limit - head
    elided = len(payload) - limit
    marker = f"\n<<TUSKER-CAPTURE-TRUNCATED: {elided} bytes elided>>\n".encode()
    return payload[:head] + marker + payload[-tail:], elided


class TurnCaptureStore:
    """Database-backed store for rejected provider turns."""

    def __init__(self, config: TurnCaptureConfig):
        self.config = config
        # Advisory: a state-database outage degrades to "not captured" instead
        # of surfacing as a failed client request.
        self._db = (
            shared_database(config.path, timeout=10.0, fallback_policy="advisory")
            if config.enabled
            else None
        )
        self._lock = threading.Lock()
        self._last_prune = 0.0
        if config.enabled:
            try:
                self._ensure_db()
            except Exception as exc:  # pragma: no cover - startup resilience
                logger.warning("turn capture state unavailable: %s", exc)

    @property
    def enabled(self) -> bool:
        return self._db is not None

    def _ensure_db(self) -> None:
        assert self._db is not None
        blob = "BYTEA" if self._db.is_postgres else "BLOB"
        with self._db.connection() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    capture_id TEXT PRIMARY KEY,
                    request_id TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    pool TEXT,
                    request_source TEXT,
                    reason TEXT NOT NULL,
                    body_policy TEXT NOT NULL,
                    replayable INTEGER NOT NULL DEFAULT 0,
                    verdict TEXT NOT NULL,
                    request_bytes INTEGER,
                    request_sha256 TEXT,
                    request_body {blob},
                    request_truncated_bytes INTEGER NOT NULL DEFAULT 0,
                    response_bytes INTEGER,
                    response_sha256 TEXT,
                    response_body {blob},
                    response_truncated_bytes INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_expires "
                f"ON {_TABLE} (expires_at)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_created "
                f"ON {_TABLE} (created_at)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_request "
                f"ON {_TABLE} (request_id)"
            )

    def _body_policy(self, pool: str | None) -> str:
        if not pool:
            return BODY_UNKNOWN_POOL
        if pool.strip().lower() in self.config.excluded_pools:
            return BODY_EXCLUDED_POOL
        return BODY_CAPTURED

    def prune(self) -> int:
        """Delete expired rows; returns the number removed."""
        if self._db is None:
            return 0
        now = time.time()
        with self._lock:
            with self._db.connection() as conn:
                cursor = conn.execute(
                    f"DELETE FROM {_TABLE} WHERE expires_at <= ?", (now,)
                )
                if conn.degraded:
                    return 0
                return int(getattr(cursor, "rowcount", 0) or 0)

    def record(
        self,
        *,
        request_id: str | None,
        provider: str,
        model: str,
        pool: str | None,
        request_source: str | None,
        reason: str,
        verdict: Mapping[str, Any],
        request_payload: bytes | None,
        response_payload: bytes | None,
    ) -> str | None:
        """Persist one rejected turn; return its capture id or ``None``.

        Returns ``None`` when the store is disabled or the write degraded, so
        callers never report a capture that was not actually stored.
        """
        if self._db is None:
            return None
        policy = self._body_policy(pool)
        now = time.time()
        capture_id = uuid.uuid4().hex

        request_body: bytes | None = None
        request_bytes: int | None = None
        request_sha256: str | None = None
        request_truncated = 0
        response_body: bytes | None = None
        response_bytes: int | None = None
        response_sha256: str | None = None
        response_truncated = 0

        if policy == BODY_CAPTURED:
            if request_payload is not None:
                request_body, request_truncated = _excerpt(
                    request_payload, self.config.max_request_bytes
                )
                request_bytes = len(request_payload)
                request_sha256 = hashlib.sha256(request_payload).hexdigest()
            else:
                policy = BODY_REQUEST_UNAVAILABLE
            if response_payload is not None:
                response_body, response_truncated = _excerpt(
                    response_payload, self.config.max_response_bytes
                )
                response_bytes = len(response_payload)
                response_sha256 = hashlib.sha256(response_payload).hexdigest()

        # Only a byte-complete request can be replayed: an excerpt would send a
        # different request to the provider.
        replayable = int(
            policy == BODY_CAPTURED
            and request_body is not None
            and request_truncated == 0
        )

        row = (
            capture_id,
            request_id,
            now,
            now + self.config.ttl_secs,
            provider,
            model,
            pool,
            request_source,
            reason,
            policy,
            replayable,
            json.dumps(dict(verdict), ensure_ascii=False, separators=(",", ":"), default=str),
            request_bytes,
            request_sha256,
            request_body,
            request_truncated,
            response_bytes,
            response_sha256,
            response_body,
            response_truncated,
        )
        with self._lock:
            if now - self._last_prune > _PRUNE_INTERVAL_SECS:
                self._last_prune = now
                try:
                    with self._db.connection() as conn:
                        conn.execute(f"DELETE FROM {_TABLE} WHERE expires_at <= ?", (now,))
                except Exception as exc:  # pragma: no cover - pruning is best effort
                    logger.warning("turn capture prune failed: %s", exc)
            try:
                with self._db.connection() as conn:
                    conn.execute(
                        f"""
                        INSERT INTO {_TABLE} (
                            capture_id, request_id, created_at, expires_at,
                            provider, model, pool, request_source, reason,
                            body_policy, replayable, verdict,
                            request_bytes, request_sha256, request_body,
                            request_truncated_bytes,
                            response_bytes, response_sha256, response_body,
                            response_truncated_bytes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        row,
                    )
                    if conn.degraded:
                        return None
            except Exception as exc:
                # Diagnostics must never turn an upstream issue into an outage.
                logger.warning("turn capture write failed: %s", exc)
                return None
        return capture_id

    def fetch(self, capture_id: str) -> dict[str, Any] | None:
        """Return one capture with raw payloads, or ``None``."""
        if self._db is None:
            return None
        with self._db.connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT capture_id, request_id, created_at, expires_at, provider,
                       model, pool, request_source, reason, body_policy, replayable,
                       verdict, request_bytes, request_sha256, request_body,
                       request_truncated_bytes, response_bytes, response_sha256,
                       response_body, response_truncated_bytes
                FROM {_TABLE} WHERE capture_id = ? AND expires_at > ?
                """,
                (capture_id, time.time()),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_captures(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        pool: str | None = None,
        reason: str | None = None,
        request_id: str | None = None,
        since: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return capture metadata (no payloads), newest first."""
        if self._db is None:
            return []
        # Retention is enforced on read as well as by pruning: a row past its
        # TTL must not be served, however long it takes for a write to reclaim it.
        clauses: list[str] = ["expires_at > ?"]
        parameters: list[Any] = [time.time()]
        for column, value in (
            ("provider", provider),
            ("model", model),
            ("pool", pool),
            ("reason", reason),
            ("request_id", request_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if since is not None:
            clauses.append("created_at >= ?")
            parameters.append(float(since))
        where = f" WHERE {' AND '.join(clauses)}"
        parameters.append(max(1, min(int(limit), 500)))
        with self._db.connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT capture_id, request_id, created_at, expires_at, provider,
                       model, pool, request_source, reason, body_policy, replayable,
                       verdict, request_bytes, request_sha256, NULL,
                       request_truncated_bytes, response_bytes, response_sha256,
                       NULL, response_truncated_bytes
                FROM {_TABLE}{where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tuple(parameters),
            )
            rows = cursor.fetchall()
        records = [self._row_to_record(row) for row in rows]
        for record in records:
            record.pop("request_body", None)
            record.pop("response_body", None)
        return records

    @staticmethod
    def _row_to_record(row: Any) -> dict[str, Any]:
        """Shape one database row into an API/CLI record.

        Bodies stay bytes so the replay tool can re-issue the exact payload;
        ``*_text`` holds the utf-8 view used by JSON surfaces.
        """
        (
            capture_id,
            request_id,
            created_at,
            expires_at,
            provider,
            model,
            pool,
            request_source,
            reason,
            body_policy,
            replayable,
            verdict_raw,
            request_bytes,
            request_sha256,
            request_body,
            request_truncated,
            response_bytes,
            response_sha256,
            response_body,
            response_truncated,
        ) = row
        try:
            verdict = json.loads(verdict_raw) if verdict_raw else {}
        except (TypeError, ValueError):
            verdict = {}
        return {
            "capture_id": capture_id,
            "request_id": request_id,
            "created_at": created_at,
            "expires_at": expires_at,
            "provider": provider,
            "model": model,
            "pool": pool,
            "request_source": request_source,
            "reason": reason,
            "body_policy": body_policy,
            "replayable": bool(replayable),
            "verdict": verdict,
            "request_bytes": request_bytes,
            "request_sha256": request_sha256,
            "request_body": bytes(request_body) if request_body is not None else None,
            "request_truncated_bytes": int(request_truncated or 0),
            "response_bytes": response_bytes,
            "response_sha256": response_sha256,
            "response_body": bytes(response_body) if response_body is not None else None,
            "response_truncated_bytes": int(response_truncated or 0),
        }


def json_safe_capture(record: dict[str, Any]) -> dict[str, Any]:
    """Return a capture record with bodies rendered for JSON transport."""
    safe = {key: value for key, value in record.items() if not key.endswith("_body")}
    for side in ("request", "response"):
        body = record.get(f"{side}_body")
        if body is None:
            safe[f"{side}_text"] = None
            safe[f"{side}_base64"] = None
            continue
        safe[f"{side}_text"] = body.decode("utf-8", errors="replace")
        safe[f"{side}_base64"] = base64.b64encode(body).decode("ascii")
    return safe


_STORE: TurnCaptureStore | None = None
_STORE_CONFIG: TurnCaptureConfig | None = None
_STORE_LOCK = threading.Lock()


def capture_store() -> TurnCaptureStore | None:
    """Return the process-wide store, rebuilding it when the policy changes.

    The policy is re-read on every call so a test or an operator can flip the
    capture switch without restarting the process, but the store object (and its
    database handle) is reused while the policy is unchanged.
    """
    global _STORE, _STORE_CONFIG
    config = load_turn_capture_config_from_env()
    if not config.enabled:
        return None
    with _STORE_LOCK:
        if _STORE is None or _STORE_CONFIG != config:
            _STORE = TurnCaptureStore(config)
            _STORE_CONFIG = config
        return _STORE


def store_turn_capture(**kwargs: Any) -> str | None:
    """Persist one rejected turn through the process-wide store."""
    store = capture_store()
    if store is None:
        return None
    return store.record(**kwargs)


def fetch_turn_capture(capture_id: str) -> dict[str, Any] | None:
    """Fetch one capture, including raw payloads, via the process-wide store."""
    store = capture_store()
    if store is None:
        return None
    return store.fetch(capture_id)


def list_turn_captures(**kwargs: Any) -> list[dict[str, Any]]:
    """List capture metadata via the process-wide store."""
    store = capture_store()
    if store is None:
        return []
    return store.list_captures(**kwargs)


def pool_capture_excluded(pool: str | None) -> bool:
    """Return whether a pool's prompt bodies are withheld from capture."""
    if not pool:
        return True
    config = load_turn_capture_config_from_env()
    return pool.strip().lower() in config.excluded_pools


__all__ = [
    "BODY_CAPTURED",
    "BODY_EXCLUDED_POOL",
    "BODY_REQUEST_UNAVAILABLE",
    "BODY_UNKNOWN_POOL",
    "TurnCaptureConfig",
    "TurnCaptureStore",
    "capture_store",
    "fetch_turn_capture",
    "json_safe_capture",
    "list_turn_captures",
    "load_turn_capture_config_from_env",
    "pool_capture_excluded",
    "store_turn_capture",
]