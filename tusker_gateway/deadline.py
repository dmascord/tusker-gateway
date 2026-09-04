"""End-to-end request deadlines with bounded client overrides."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from typing import Mapping

from aiohttp import web

from tusker_gateway.errors import openai_error


@dataclass(frozen=True)
class DeadlineConfig:
    default_timeout_ms: int = 120_000
    max_timeout_ms: int = 300_000
    allow_client_override: bool = True

    @property
    def enabled(self) -> bool:
        return self.default_timeout_ms > 0 and self.max_timeout_ms > 0


def _positive_int(value: str, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def load_deadline_config_from_env(
    env: Mapping[str, str] | None = None,
) -> DeadlineConfig:
    env = os.environ if env is None else env
    default = _positive_int(env.get("TUSKER_REQUEST_TIMEOUT_MS", "120000"), 120_000)
    maximum = _positive_int(env.get("TUSKER_MAX_REQUEST_TIMEOUT_MS", "300000"), 300_000)
    if default and maximum:
        default = min(default, maximum)
    allow_override = env.get(
        "TUSKER_ALLOW_CLIENT_TIMEOUT", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}
    return DeadlineConfig(default, maximum, allow_override)


def _request_timeout_ms(request: web.Request, config: DeadlineConfig) -> int:
    timeout_ms = config.default_timeout_ms
    supplied = request.headers.get("X-Tusker-Timeout-Ms", "").strip()
    if not supplied or not config.allow_client_override:
        return timeout_ms
    try:
        requested = int(supplied)
    except ValueError as exc:
        raise ValueError("X-Tusker-Timeout-Ms must be an integer") from exc
    if requested <= 0:
        raise ValueError("X-Tusker-Timeout-Ms must be greater than zero")
    return min(requested, config.max_timeout_ms)


def attach_deadline_middleware(app: web.Application, config: DeadlineConfig) -> None:
    if not config.enabled:
        return

    async def mark_prepared(
        request: web.Request, response: web.StreamResponse
    ) -> None:
        request["_response_prepared"] = True

    app.on_response_prepare.append(mark_prepared)

    @web.middleware
    async def deadline_middleware(request: web.Request, handler):
        if not request.path.startswith("/v1/"):
            return await handler(request)
        try:
            timeout_ms = _request_timeout_ms(request, config)
        except ValueError as exc:
            return web.json_response(
                openai_error(
                    str(exc), code="invalid_request_timeout", error_type="invalid_request_error"
                ),
                status=400,
            )
        request["_deadline_ms"] = timeout_ms
        request["_deadline_at"] = asyncio.get_running_loop().time() + timeout_ms / 1000
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                response = await handler(request)
        except TimeoutError:
            request["_deadline_exceeded"] = True
            if request.get("_response_prepared"):
                # The status line is already on the wire. Closing produces a
                # clear truncated-stream failure instead of a malformed second
                # HTTP response.
                if request.transport is not None:
                    request.transport.close()
                raise
            return web.json_response(
                openai_error(
                    "Gateway request deadline exceeded",
                    code="request_timeout",
                    error_type="server_error",
                ),
                status=504,
                headers={"X-Tusker-Timeout-Ms": str(timeout_ms)},
            )
        if not response.prepared:
            response.headers["X-Tusker-Timeout-Ms"] = str(timeout_ms)
        return response

    app.middlewares.append(deadline_middleware)


__all__ = [
    "DeadlineConfig",
    "attach_deadline_middleware",
    "load_deadline_config_from_env",
]
