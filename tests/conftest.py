"""Shared test fixtures for tusker-gateway tests."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.config import load_config
from tusker_gateway.endpoints import (
    chat_completions_handler,
    models_handler,
    rerank_handler,
    responses_handler,
)
from tusker_gateway.anthropic_adapter import anthropic_messages_handler
from tusker_gateway.errors import GatewayError, openai_error
from tusker_gateway.health import health_handler, ready_handler, status_handler


@pytest.fixture(autouse=True)
def setup_auth_file(tmp_path, monkeypatch):
    """Point TUSKER_AUTH_FILE at an isolated temp file so tests don't pollute
    the developer's real ~/.hermes/auth.json."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"version": 1, "credential_pool": {}}))
    monkeypatch.setenv("TUSKER_AUTH_FILE", str(auth_file))
    # Test app expects HEADERS_AUTH = "Bearer sk-secret-dev" to work, so make
    # that the only accepted key for tests that don't override api_keys.
    monkeypatch.setenv("API_KEYS", "sk-secret-dev")
    return auth_file


@pytest.fixture(autouse=True)
def reset_cooldown_tracker():
    """Clear the global cooldown tracker between tests so a 429 in one test
    doesn't poison subsequent tests' pool selection."""
    from tusker_gateway.cooldown import global_tracker
    tracker = global_tracker()
    tracker._cooldowns.clear()
    tracker._provider_default.clear()
    tracker._group_cooldowns.clear()
    tracker._recent_failures.clear()
    tracker._global = None
    yield
    tracker._cooldowns.clear()
    tracker._provider_default.clear()
    tracker._group_cooldowns.clear()
    tracker._recent_failures.clear()
    tracker._global = None
    from tusker_gateway.provider_usage import capacity_controller
    capacity_controller().reset()


@pytest.fixture(autouse=True)
def restore_provider_endpoints():
    """Snapshot PROVIDER_ENDPOINTS so test patches can be restored even if a
    test fails before its finally clause runs."""
    from tusker_gateway.passthrough import PROVIDER_ENDPOINTS
    snapshot = {k: dict(v) for k, v in PROVIDER_ENDPOINTS.items()}
    yield
    PROVIDER_ENDPOINTS.clear()
    PROVIDER_ENDPOINTS.update(snapshot)


def _create_test_app(config=None):
    cfg = config or load_config()
    # Default test config: only accept the well-known dev key so the test
    # client fixtures (HEADERS_AUTH) work without per-test setup. Tests that
    # need a different auth scheme override cfg["api_keys"] explicitly.
    if not cfg.get("api_keys"):
        cfg["api_keys"] = ["sk-secret-dev"]
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
    app.router.add_post("/v1/messages", anthropic_messages_handler)
    app.router.add_post("/v1/rerank", rerank_handler)
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
