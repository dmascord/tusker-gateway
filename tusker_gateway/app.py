"""Application factory: assemble aiohttp app from modules."""

from __future__ import annotations

import aiohttp
from aiohttp import web

from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.config import load_config
from tusker_gateway.endpoints import (
    chat_completions_handler,
    models_handler,
    responses_handler,
)
from tusker_gateway.errors import GatewayError, openai_error
from tusker_gateway.health import health_handler, ready_handler, status_handler


def create_app() -> web.Application:
    """Build and return the aiohttp Application."""
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app["config"] = load_config()

    async def on_startup(app):
        config = app["config"]
        app["http_session"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
        )
        # Hydrate persistent cooldowns into the in-memory tracker
        try:
            from pathlib import Path as _P
            from tusker_gateway.cooldown import global_tracker
            from tusker_gateway.persistent_cooldown import PersistentCooldownStore
            db_dir = _P(config.get("quality_db_path", "data/quality.db")).parent
            store = PersistentCooldownStore(db_path=db_dir / "cooldowns.db")
            store.hydrate(global_tracker())
            store.hydrate_providers(global_tracker())
            store.purge_expired()
        except Exception:
            pass  # best-effort

    auth = AuthMiddleware()

    @web.middleware
    async def auth_middleware(request, handler):
        if request.path in ("/health", "/ready"):
            return await handler(request)
        try:
            await auth.verify(request)
        except GatewayError as exc:
            return web.json_response(
                openai_error(exc.message, code=exc.code, error_type=exc.error_type),
                status=exc.status,
            )
        return await handler(request)

    app.middlewares.append(auth_middleware)

    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)
    app.router.add_get("/status", status_handler)
    app.router.add_get("/v1/models", models_handler)
    app.router.add_post("/v1/chat/completions", chat_completions_handler)
    app.router.add_post("/v1/responses", responses_handler)

    async def on_cleanup(app):
        if "http_session" in app:
            await app["http_session"].close()

    app.on_cleanup.append(on_cleanup)
    app.on_startup.append(on_startup)
    return app


def main() -> None:
    config = load_config()
    app = create_app()
    web.run_app(app, host=config["host"], port=config["port"])


if __name__ == "__main__":
    main()