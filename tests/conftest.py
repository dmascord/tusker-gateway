"""Shared test fixtures for tusker-gateway tests."""
from __future__ import annotations

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.config import load_config
from tusker_gateway.endpoints import (
    chat_completions_handler,
    models_handler,
    responses_handler,
)
@pytest.fixture(autouse=True)
def setup_auth_file(tmp_path):
    import json
    from pathlib import Path
    auth_file = Path.home() / ".hermes" / "auth.json"
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(json.dumps([{"access_token": "mock"}]))
from tusker_gateway.errors import GatewayError, openai_error
from tusker_gateway.health import health_handler, ready_handler, status_handler


def _create_test_app(config=None):
    cfg = config or load_config()
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app["config"] = cfg
    app["http_session"] = None

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
    return app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def app():
    return _create_test_app()


@pytest_asyncio.fixture
async def client(app):
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


HEADERS_AUTH = {"Authorization": "Bearer sk-secret-dev"}
HEADERS_NO_AUTH = {}
