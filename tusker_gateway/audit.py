"""Append-only, integrity-chained audit events for gateway requests."""
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

logger = logging.getLogger(__name__)

_GENESIS_HASH = "0" * 64

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

    @property
    def enabled(self) -> bool:
        return bool(self.path)


def _bool_env(value: str, *, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_audit_config_from_env(env: Mapping[str, str] | None = None) -> AuditConfig:
    env = os.environ if env is None else env
    excluded = tuple(
        item.strip()
        for item in env.get("TUSKER_AUDIT_EXCLUDE_PATHS", "/health,/ready,/metrics").split(",")
        if item.strip()
    )
    return AuditConfig(
        path=env.get("TUSKER_AUDIT_LOG_PATH", "").strip(),
        hmac_key=env.get("TUSKER_AUDIT_HMAC_KEY", ""),
        fail_closed=_bool_env(env.get("TUSKER_AUDIT_FAIL_CLOSED", ""), default=False),
        fsync=_bool_env(env.get("TUSKER_AUDIT_FSYNC", ""), default=True),
        exclude_paths=excluded,
    )


class AuditWriteError(RuntimeError):
    """Raised when fail-closed audit persistence cannot complete."""


class AuditLogger:
    """Write JSONL audit records whose hashes form an append-only chain."""

    def __init__(self, config: AuditConfig):
        self.config = config

    def _digest(self, previous_hash: str, canonical_event: bytes) -> str:
        message = previous_hash.encode("ascii") + b"." + canonical_event
        if self.config.hmac_key:
            return hmac.new(
                self.config.hmac_key.encode("utf-8"), message, hashlib.sha256
            ).hexdigest()
        return hashlib.sha256(message).hexdigest()

    @staticmethod
    def _previous_hash(handle) -> str:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return _GENESIS_HASH
        # Audit events are deliberately bounded metadata records. Reading the
        # final 64 KiB keeps append cost constant as retention files grow.
        handle.seek(max(0, size - 65_536))
        lines = [line for line in handle.read().splitlines() if line.strip()]
        if not lines:
            return _GENESIS_HASH
        record = json.loads(lines[-1].decode("utf-8"))
        candidate = record.get("hash")
        if not isinstance(candidate, str) or len(candidate) != 64:
            raise AuditWriteError("audit log contains an invalid chain record")
        return candidate

    def _append(self, event: Mapping[str, Any]) -> dict[str, Any]:
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
                payload = dict(event)
                payload["chain_version"] = 1
                payload["previous_hash"] = previous_hash
                payload["integrity"] = (
                    "hmac-sha256" if self.config.hmac_key else "sha256"
                )
                canonical = json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
                payload["hash"] = self._digest(previous_hash, canonical)
                handle.seek(0, os.SEEK_END)
                handle.write(
                    (json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode(
                        "utf-8"
                    )
                )
                handle.flush()
                if self.config.fsync:
                    os.fsync(handle.fileno())
                return payload
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    async def write(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        if not self.config.enabled:
            return None
        try:
            return await asyncio.to_thread(self._append, event)
        except Exception as exc:
            logger.error("audit write failed: %s", exc.__class__.__name__)
            if self.config.fail_closed:
                raise AuditWriteError("required audit persistence failed") from exc
            return None

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
                    if not hmac.compare_digest(claimed, verifier._digest(previous, canonical)):
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
