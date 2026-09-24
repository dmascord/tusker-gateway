"""Isolated HTTP worker for Kilo CLI requests; intended for a dedicated K8s node."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from aiohttp import web

from tusker_gateway.errors import BadRequestError, ProviderError, ProviderRouteDisabledError
from tusker_gateway.provider_adapters.kilo_cli import KiloCLIAdapter

logger = logging.getLogger("tusker_gateway.kilo_worker")
_ALLOWED_MODELS = frozenset({
    "groq/openai/gpt-oss-20b",
    "groq/qwen/qwen3.8-27b",
    "kilo/kilo-auto/free",
})
_CONCURRENCY = asyncio.Semaphore(2)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "worker": "kilo"})


async def chat(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (ValueError, web.HTTPException):
        raise web.HTTPBadRequest(text="invalid JSON request")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be an object")
    model = body.get("model")
    messages = body.get("messages")
    if model not in _ALLOWED_MODELS:
        return web.json_response(
            {"error": {"code": "unsupported_model", "message": "Kilo model is not enabled on this worker"}},
            status=400,
        )
    if not isinstance(messages, list) or not messages or not all(isinstance(item, dict) for item in messages):
        raise web.HTTPBadRequest(text="messages must be a non-empty array of objects")

    adapter = KiloCLIAdapter()
    async with _CONCURRENCY:
        try:
            response_model = body.get("public_model")
            if not isinstance(response_model, str) or not response_model.startswith("kilo-cli/"):
                response_model = model
            result: Any = await adapter.chat(
                provider="kilo-cli",
                model=response_model,
                messages=messages,
                stream=body.get("stream") is True,
                tools=body.get("tools"),
                tool_choice=body.get("tool_choice"),
            )
        except (BadRequestError, ProviderError, ProviderRouteDisabledError) as exc:
            status = 503 if isinstance(exc, (ProviderError, ProviderRouteDisabledError)) else 400
            return web.json_response(
                {"error": {"code": getattr(exc, "code", "kilo_worker_failed"), "message": str(exc)}},
                status=status,
            )
        except Exception as exc:  # keep worker failures private; log class, not request content
            logger.exception("unexpected Kilo worker failure: %s", type(exc).__name__)
            return web.json_response(
                {"error": {"code": "kilo_worker_failed", "message": "Kilo worker request failed"}},
                status=502,
            )
        if hasattr(result, "__aiter__"):
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            )
            await response.prepare(request)
            try:
                async for frame in result:
                    await response.write(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Kilo stream failed: %s", getattr(exc, "code", type(exc).__name__))
                error = {
                    "error": {
                        "message": str(exc) or "Kilo stream failed",
                        "type": "provider_error",
                        "code": getattr(exc, "code", "kilo_worker_failed"),
                    },
                }
                await response.write(f"data: {json.dumps(error)}\n\ndata: [DONE]\n\n".encode())
            return response
    return web.json_response(result)


def create_app() -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.router.add_get("/healthz", health)
    app.router.add_post("/v1/chat/completions", chat)
    return app


def main() -> None:
    os.environ.setdefault("TUSKER_KILO_CLI_ENABLED", "true")
    web.run_app(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8643")))


if __name__ == "__main__":
    main()
