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
from tusker_gateway.mcp_guard import mcp_handler


@pytest.fixture(autouse=True)
def setup_auth_file(tmp_path, monkeypatch):
    """Point TUSKER_AUTH_FILE at an isolated temp file so tests don't pollute
    the developer's real ~/.hermes/auth.json."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"version": 1, "credential_pool": {}}))
    monkeypatch.setenv("TUSKER_AUTH_FILE", str(auth_file))
    # Explicit test credentials. Production auth has no hardcoded dev-key
    # bypass; this value is supplied through test configuration only.
    monkeypatch.setenv("API_KEYS", "sk-secret-dev")
    monkeypatch.setenv("TUSKER_APPROVAL_HMAC_KEY", "test-approval-hmac-key")
    return auth_file


@pytest.fixture(autouse=True)
def reset_cooldown_tracker():
    """Clear the global cooldown tracker between tests so a 429 in one test
    doesn't poison subsequent tests' pool selection."""
    from tusker_gateway.cooldown import global_tracker, PERMANENTLY_FAILED_MODELS
    tracker = global_tracker()
    tracker._cooldowns.clear()
    tracker._provider_default.clear()
    tracker._group_cooldowns.clear()
    tracker._recent_failures.clear()
    tracker._global = None
    PERMANENTLY_FAILED_MODELS.clear()
    yield
    tracker._cooldowns.clear()
    tracker._provider_default.clear()
    tracker._group_cooldowns.clear()
    tracker._recent_failures.clear()
    tracker._global = None
    PERMANENTLY_FAILED_MODELS.clear()
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
    # Tests must explicitly configure api_keys. load_config() generates a random
    # key when API_KEYS is unset, which ensures the gateway is never configured
    # with a dev key by default.
    if not cfg.get("api_keys"):
        raise RuntimeError(
            "_create_test_app requires explicitly configured API_KEYS; "
            "do not default to sk-secret-dev"
        )
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
    app.router.add_post("/mcp", mcp_handler)
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
