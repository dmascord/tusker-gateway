"""Error types and OpenAI-compatible error responses."""
from __future__ import annotations

import os
from typing import Any


class GatewayError(Exception):
    """Base error with an HTTP status and OpenAI error shape."""

    status: int = 500
    error_type: str = "server_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code


class AuthenticationError(GatewayError):
    status = 401
    error_type = "invalid_request_error"

    def __init__(self, message: str = "Invalid API key", *, code: str | None = "invalid_api_key"):
        super().__init__(message, code=code)


class AuthorizationError(GatewayError):
    status = 403
    error_type = "permission_error"

    def __init__(self, message: str = "Forbidden", *, code: str | None = "forbidden"):
        super().__init__(message, code=code)


class BadRequestError(GatewayError):
    status = 400
    error_type = "invalid_request_error"


class NoHealthyModelsError(BadRequestError):
    """A pool has no currently eligible upstream candidate.

    This is usually a transient availability condition caused by provider or
    model cooldowns, request capabilities, or breaker state. Keep it as a
    ``BadRequestError`` subclass so existing handler plumbing catches it, but
    report it as a retryable service condition rather than a client error.
    """

    status = 503
    error_type = "server_error"

    def __init__(self, *, pool: str | None = None) -> None:
        super().__init__(
            "No healthy upstream model is currently available; retry shortly.",
            code="no_healthy_models",
        )
        self.pool = pool
        try:
            retry_after = max(
                1,
                int(float(os.environ.get("TUSKER_PROVIDER_RETRY_AFTER_SECS", "5"))),
            )
        except (TypeError, ValueError):
            retry_after = 5
        self.headers = {"Retry-After": str(retry_after)}


class NotFoundError(GatewayError):
    status = 404
    error_type = "invalid_request_error"


class ProviderError(GatewayError):
    """An upstream provider failure. status maps to a client-safe code."""

    status = 502
    error_type = "provider_error"


class ProviderCapacityError(ProviderError):
    """A provider worker-pool capacity failure handled by pool fallback."""

    status = 503
    error_type = "server_error"

    def __init__(
        self,
        *,
        group: str,
        detail: str | None = None,
        capacity_rejected: bool = False,
    ) -> None:
        # ``detail`` is retained for redacted operational logs. Endpoint
        # handlers must use the generic public message below instead.
        super().__init__(
            detail or "Upstream provider capacity is temporarily unavailable",
            code="provider_capacity",
        )
        self.capacity_group = group
        self.capacity_rejected = capacity_rejected
        self.upstream_status = 503
        self.upstream_body = detail or "provider capacity unavailable"


class MalformedToolCallError(ProviderError):
    """An upstream model emitted tool markup that could not be parsed safely."""

    def __init__(
        self,
        *,
        marker_types: tuple[str, ...] = (),
    ) -> None:
        markers = ",".join(marker_types) or "unknown"
        super().__init__(
            "Provider emitted malformed tool-call markup",
            code="malformed_tool_call",
        )
        # These attributes let the normal provider-fallback path cool down and
        # exclude the candidate without exposing model output to the caller.
        self.upstream_status = 502
        self.upstream_body = f"malformed tool-call markup: {markers}"
        self.marker_types = marker_types


class InvalidToolCallArgumentsError(ProviderError):
    """An upstream tool call did not satisfy the requested tool schema."""

    def __init__(
        self,
        *,
        tool_name: str,
        reason: str,
        missing: tuple[str, ...] = (),
        argument_chars: int = 0,
    ) -> None:
        super().__init__(
            "Provider emitted invalid tool-call arguments",
            code="invalid_tool_call_arguments",
        )
        # Keep operational details bounded and free of argument values. Tool
        # arguments can contain paths, prompts, credentials, or source code.
        missing_text = ",".join(missing) or "none"
        self.upstream_status = 502
        self.upstream_body = (
            f"invalid tool-call arguments: tool={tool_name}; reason={reason}; "
            f"missing={missing_text}; argument_chars={max(0, argument_chars)}"
        )
        self.tool_name = tool_name
        self.reason = reason
        self.missing = missing
        self.argument_chars = max(0, argument_chars)


class RequiredToolCallError(ProviderError):
    """An upstream model ended without honoring ``tool_choice=required``."""

    def __init__(self) -> None:
        super().__init__(
            "Provider did not produce a required tool call",
            code="required_tool_call_missing",
        )
        self.upstream_status = 502
        self.upstream_body = "required tool call missing"


class UnusableToolResponseError(ProviderError):
    """An upstream model produced no client-visible answer for a tool turn."""

    def __init__(self, *, reason: str, reasoning_chars: int = 0) -> None:
        super().__init__(
            "Provider returned no usable assistant response",
            code="unusable_tool_response",
        )
        self.upstream_status = 502
        self.upstream_body = (
            f"unusable tool response: {reason}; reasoning_chars={max(0, reasoning_chars)}"
        )
        self.reason = reason
        self.reasoning_chars = max(0, reasoning_chars)


class RateLimitError(GatewayError):
    """A rate-limit / cooldown hit on a provider pool."""

    status = 429
    error_type = "rate_limit_exceeded"

    def __init__(self, message: str = "Rate limited", *, code: str | None = "rate_limit_exceeded", body: str | None = None, headers: dict[str, str] | None = None):
        super().__init__(message, code=code)
        self.body = body
        self.headers = headers or {}

def openai_error(message: str, *, code: str | None, error_type: str) -> dict[str, Any]:
    """Build an OpenAI-style error response body."""
    err: dict[str, Any] = {"type": error_type, "message": message}
    if code:
        err["code"] = code
    return {"error": err}


def http_json_response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Helper to build an aiohttp json response from an error."""
    return {"status": status, "body": body}
