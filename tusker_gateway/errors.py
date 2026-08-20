"""Error types and OpenAI-compatible error responses."""
from __future__ import annotations

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


class BadRequestError(GatewayError):
    status = 400
    error_type = "invalid_request_error"


class NotFoundError(GatewayError):
    status = 404
    error_type = "invalid_request_error"


class ProviderError(GatewayError):
    """An upstream provider failure. status maps to a client-safe code."""

    status = 502
    error_type = "provider_error"


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
