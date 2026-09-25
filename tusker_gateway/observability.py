"""Observability: access logging, request-ID propagation, structured metrics.

Provides structured access logging per HTTP request with correlation IDs,
latency tracking, and upstream model selection information.
"""

import ipaddress
import json
import logging
import os
import secrets
import time
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


def client_ip(request: web.Request) -> str:
    """Return the real client IP for a request.

    Prioritises:
    1. CF-Connecting-IP (Cloudflare) — *only* when the peer is a trusted proxy.
    2. X-Forwarded-For leftmost — same trust gate.
    3. The direct peer address — returned unchanged when the peer is not a
       trusted proxy, so untrusted callers cannot spoof their logged IP.
    """
    peer = request.remote
    if peer and _is_trusted_proxy(peer):
        cf_ip = request.headers.get("CF-Connecting-IP", "")
        if cf_ip:
            return cf_ip.strip()
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return peer or "unknown"


# Trusted proxy networks whose forwarded client-IP headers are honoured.
# A request's CF-Connecting-IP / X-Forwarded-For is trusted only when the
# direct peer itself belongs to one of these networks; otherwise the headers
# are ignored so untrusted callers cannot spoof logged or audited IPs.
_TRUSTED_CIDRS = [
    ipaddress.ip_network(cidr.strip(), strict=False)
    for cidr in os.environ.get(
        "TUSKER_TRUSTED_PROXY_RANGES",
        # Cloudflare IPv4 + IPv6 ranges. Override with your own proxy network
        # (e.g. an in-cluster ingress CIDR) when not fronted by Cloudflare.
        "173.245.48.0/20,103.21.244.0/20,141.101.64.0/18,198.41.128.0/17,"
        "162.158.0.0/10,104.16.0.0/12,104.17.0.0/15,104.18.0.0/14,"
        "104.19.0.0/16,104.20.0.0/14,104.24.0.0/14,104.28.0.0/14,"
        "104.30.0.0/15,104.32.0.0/11,2606:4700::/32,2803:f800::/32,"
        "2400:cb00::/32,2a06:98c0::/29,2c0f:fb50::/32",
    ).split(",")
    if cidr.strip()
]


def _is_trusted_proxy(ip_str: str) -> bool:
    """Return True when *ip_str* belongs to a configured trusted proxy network."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in cidr for cidr in _TRUSTED_CIDRS)


_ACCESS_LOG_CONTEXT_KEY = "_access_log_context"
_ACCESS_LOG_FIELDS = frozenset({
    "provider",
    "model",
    "pool",
    "route_kind",
    "requested_model",
    "candidate_attempts",
    "failure_class",
    "cache_status",
        "tokens_in",
        "tokens_out",
        "error_detail",
})


def set_access_log_context(request: web.Request, **fields: Any) -> None:
    """Attach known routing/cache fields to this request's access record.

    Handlers learn these values at different stages (routing, cache lookup,
    or upstream completion).  Ignore unknown fields and retain earlier
    non-None values so a later partial update cannot erase useful context.
    """
    # Some unit-level callers provide a lightweight request stand-in with
    # only ``app``.  Context enrichment must never change routing behavior.
    if not hasattr(request, "get"):
        return
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
        route_kind: str | None = None,
        requested_model: str | None = None,
        candidate_attempts: int | None = None,
        cache_status: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        error: str | None = None,
        error_detail: str | None = None,
        failure_class: str | None = None,
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
            "client_ip": client_ip(request),
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
        if route_kind:
            record["route_kind"] = route_kind
        if requested_model:
            record["requested_model"] = requested_model
        if candidate_attempts is not None:
            record["candidate_attempts"] = candidate_attempts

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
        if error_detail:
            record["error_detail"] = str(error_detail)[:512]
        if failure_class:
            record["failure_class"] = failure_class

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
            stream_error = request.get("_stream_error")
            access_context = _access_log_context(request)
            if stream_error:
                access_context["error"] = stream_error
            if request.get("_stream_error_detail"):
                # A stream failure may already have stored a redacted
                # provider detail in the routing context. The client-facing
                # stream detail is more actionable, so use it as the final
                # value without passing duplicate keyword arguments.
                access_context["error_detail"] = request["_stream_error_detail"]
            access_log.log(
                request,
                response.status,
                (time.monotonic() - started) * 1000,
                **access_context,
            )
        # StreamResponse headers are immutable after prepare(). Streaming
        # handlers must set X-Request-ID in their initial headers.
        if not response.prepared:
            response.headers["X-Request-ID"] = request_id
        return response

    # Insert before auth middleware so ID is available everywhere
    app.middlewares.insert(0, request_id_middleware)
