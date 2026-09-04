"""Observability: access logging, request-ID propagation, structured metrics.

Provides structured access logging per HTTP request with correlation IDs,
latency tracking, and upstream model selection information.
"""

import json
import logging
import os
import secrets
import time
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

_ACCESS_LOG_CONTEXT_KEY = "_access_log_context"
_ACCESS_LOG_FIELDS = frozenset({
    "provider",
    "model",
    "pool",
    "cache_status",
    "tokens_in",
    "tokens_out",
})


def set_access_log_context(request: web.Request, **fields: Any) -> None:
    """Attach known routing/cache fields to this request's access record.

    Handlers learn these values at different stages (routing, cache lookup,
    or upstream completion).  Ignore unknown fields and retain earlier
    non-None values so a later partial update cannot erase useful context.
    """
    context = request.get(_ACCESS_LOG_CONTEXT_KEY)
    if not isinstance(context, dict):
        context = {}
        request[_ACCESS_LOG_CONTEXT_KEY] = context
    for name, value in fields.items():
        if name in _ACCESS_LOG_FIELDS and value is not None:
            context[name] = value


def _access_log_context(request: web.Request) -> dict[str, Any]:
    """Return only fields accepted by :meth:`AccessLog.log`."""
    context = request.get(_ACCESS_LOG_CONTEXT_KEY)
    if not isinstance(context, dict):
        return {}
    return {
        name: context[name]
        for name in _ACCESS_LOG_FIELDS
        if name in context
    }


def get_access_log_context(request: web.Request) -> dict[str, Any]:
    """Return a copy of the bounded routing context for audit integrations."""
    return _access_log_context(request)


def _generate_request_id() -> str:
    """Generate a unique request ID for correlation."""
    return f"req_{secrets.token_hex(8)}"


def _is_access_logging_enabled() -> bool:
    """Return whether structured access logging is enabled via env."""
    val = os.environ.get("TUSKER_ACCESS_LOG", "1").strip().lower()
    return val not in {"0", "false", "no", "off"}


class AccessLog:
    """Structured access logger for HTTP requests with request-ID correlation."""

    def __init__(self):
        self.enabled = _is_access_logging_enabled()
        self.logger = logging.getLogger("tusker_gateway.access")

    def log(
        self,
        request: web.Request,
        response_status: int,
        latency_ms: float,
        provider: str | None = None,
        model: str | None = None,
        pool: str | None = None,
        cache_status: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        error: str | None = None,
    ) -> None:
        """Log a structured access record for one request.

        Args:
            request: aiohttp request object.
            response_status: HTTP status code of response.
            latency_ms: Request latency in milliseconds.
            provider: Upstream provider selected (e.g., 'openrouter').
            model: Upstream model selected.
            pool: Pool name if applicable (e.g., 'code', 'privacy').
            cache_status: 'hit', 'miss', or None if caching not applicable.
            tokens_in: Input tokens if known.
            tokens_out: Output tokens if known.
            error: Error message or code if request failed.
        """
        if not self.enabled:
            return

        request_id = request.get("_request_id", "unknown")
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "request_id": request_id,
            "method": request.method,
            "path": request.path,
            "status": response_status,
            "latency_ms": round(latency_ms, 1),
        }

        identity = request.get("identity")
        if identity is not None:
            record["principal"] = getattr(identity, "principal", "unknown")
            record["tenant"] = getattr(identity, "tenant", "unknown")
            record["key_fingerprint"] = getattr(
                identity, "key_fingerprint", "unknown"
            )

        # Upstream details
        if provider:
            record["provider"] = provider
        if model:
            record["model"] = model
        if pool:
            record["pool"] = pool

        # Token usage (from response body or context)
        if tokens_in is not None or tokens_out is not None:
            usage = {}
            if tokens_in is not None:
                usage["in"] = tokens_in
            if tokens_out is not None:
                usage["out"] = tokens_out
            if usage:
                record["usage"] = usage

        # Cache telemetry
        if cache_status:
            record["cache"] = cache_status

        # Error if applicable
        if error:
            record["error"] = error

        self.logger.info(json.dumps(record))


def attach_request_id_middleware(app: web.Application) -> None:
    """Attach middleware that generates/extracts request IDs and returns them.

    Request IDs are extracted from X-Request-ID header or generated if missing.
    Attached to the request as _request_id for downstream handlers.
    Returned in response headers as X-Request-ID.
    """

    @web.middleware
    async def request_id_middleware(request, handler):
        started = time.monotonic()
        # Extract or generate request ID
        request_id = request.headers.get("X-Request-ID", "").strip()
        if not request_id:
            request_id = _generate_request_id()

        # Bound caller-controlled IDs so logs and downstream systems cannot
        # receive unbounded correlation values.
        request_id = request_id[:128]

        # Attach to request for downstream use
        request["_request_id"] = request_id

        access_log = app.get("access_log")
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            if access_log is not None:
                access_log.log(
                    request,
                    exc.status,
                    (time.monotonic() - started) * 1000,
                    **_access_log_context(request),
                    error=exc.__class__.__name__,
                )
            raise
        except Exception as exc:
            if access_log is not None:
                access_log.log(
                    request,
                    504 if request.get("_deadline_exceeded") else 500,
                    (time.monotonic() - started) * 1000,
                    **_access_log_context(request),
                    error=exc.__class__.__name__,
                )
            raise

        if access_log is not None:
            access_log.log(
                request,
                response.status,
                (time.monotonic() - started) * 1000,
                **_access_log_context(request),
            )
        # StreamResponse headers are immutable after prepare(). Streaming
        # handlers must set X-Request-ID in their initial headers.
        if not response.prepared:
            response.headers["X-Request-ID"] = request_id
        return response

    # Insert before auth middleware so ID is available everywhere
    app.middlewares.insert(0, request_id_middleware)
