"""Coverage for the read-only admin turn-capture endpoints."""
from __future__ import annotations

import base64
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway import turn_capture
from tusker_gateway.admin import (
    attach_admin_access_middleware,
    register_admin_config_routes,
)
from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
from tusker_gateway.errors import GatewayError, openai_error
from tusker_gateway.identity import (
    IdentityStore,
    attach_authorization_middleware,
    fingerprint_api_key,
    load_identity_config_from_env,
)
from tusker_gateway.turn_capture import TurnCaptureConfig, TurnCaptureStore


_ADMIN_KEY = "sk-admin-captures"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_KEY}"}


def _auth_middleware(store: IdentityStore):
    auth = AuthMiddleware(store)

    @web.middleware
    async def middleware(request, handler):
        if request.path == "/admin" or request.path.startswith("/admin/"):
            return await handler(request)
        try:
            await auth.verify(request)
        except GatewayError as exc:
            return web.json_response(
                openai_error(exc.message, code=exc.code, error_type=exc.error_type),
                status=exc.status,
            )
        return await handler(request)

    return middleware


def _admin_identities():
    fingerprint = fingerprint_api_key(_ADMIN_KEY)
    return load_identity_config_from_env({
        "TUSKER_IDENTITIES_JSON": json.dumps({
            fingerprint: {
                "principal": "admin-captures",
                "tenant": "operations",
                "scopes": ["admin:read"],
            }
        })
    })


def _admin_app() -> web.Application:
    app = web.Application()
    app["config"] = {
        "api_keys": [_ADMIN_KEY],
        "providers": DEFAULT_PROVIDER_REGISTRY,
        "excluded_providers": [],
        "disabled_providers": [],
    }
    store = IdentityStore(_admin_identities())
    app["identity_store"] = store
    app.middlewares.append(_auth_middleware(store))
    attach_authorization_middleware(app)
    attach_admin_access_middleware(app)
    register_admin_config_routes(app)
    return app


async def _client(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
def capture_store(tmp_path, monkeypatch):
    """A real temp-file store shared with the process-wide capture store.

    The handlers resolve captures through ``tusker_gateway.turn_capture``'s
    process-wide store, so the policy is pointed at the same sqlite file the
    test seeds; a blank state DSN forces the sqlite path over PostgreSQL.
    """
    db_path = tmp_path / "captures.db"
    monkeypatch.setattr(turn_capture, "_STORE", None)
    monkeypatch.setattr(turn_capture, "_STORE_CONFIG", None)
    monkeypatch.setenv("TUSKER_STREAM_CAPTURE", "1")
    monkeypatch.setenv("TUSKER_TURN_CAPTURE_PATH", str(db_path))
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    monkeypatch.setenv("TUSKER_DATABASE_URL", "")
    store = TurnCaptureStore(TurnCaptureConfig(enabled=True, path=str(db_path)))
    yield store
    monkeypatch.setattr(turn_capture, "_STORE", None)
    monkeypatch.setattr(turn_capture, "_STORE_CONFIG", None)


def _seed(
    store: TurnCaptureStore,
    *,
    request_id: str,
    provider: str,
    model: str,
    pool: str,
    reason: str,
    request: bytes = b'{"prompt":"secret"}',
    response: bytes = b'{"ok":true}',
) -> str:
    capture_id = store.record(
        request_id=request_id,
        provider=provider,
        model=model,
        pool=pool,
        request_source="chat",
        reason=reason,
        verdict={"status": "rejected"},
        request_payload=request,
        response_payload=response,
    )
    assert capture_id is not None
    return capture_id


async def test_list_returns_metadata_without_payload_text(capture_store):
    capture_id = _seed(
        capture_store,
        request_id="req-a",
        provider="openrouter",
        model="m1",
        pool="code",
        reason="qualification_failed",
    )
    app = _admin_app()
    client = await _client(app)
    try:
        resp = await client.get("/admin/captures", headers=_HEADERS)
        assert resp.status == 200
        text = await resp.text()
        assert "secret" not in text
        data = json.loads(text)
        assert data["count"] == 1
        row = data["captures"][0]
        assert row["capture_id"] == capture_id
        assert row["request_id"] == "req-a"
        assert "request_text" not in row
        assert "request_body" not in row
        assert "response_body" not in row
    finally:
        await client.close()


async def test_list_filters_by_reason_and_provider(capture_store):
    _seed(
        capture_store,
        request_id="keep",
        provider="openrouter",
        model="m1",
        pool="code",
        reason="qualification_failed",
    )
    _seed(
        capture_store,
        request_id="drop",
        provider="closedai",
        model="m2",
        pool="code",
        reason="other",
    )
    app = _admin_app()
    client = await _client(app)
    try:
        resp = await client.get(
            "/admin/captures?reason=qualification_failed", headers=_HEADERS
        )
        data = await resp.json()
        assert data["count"] == 1
        assert data["captures"][0]["request_id"] == "keep"

        resp = await client.get("/admin/captures?provider=closedai", headers=_HEADERS)
        data = await resp.json()
        assert [c["request_id"] for c in data["captures"]] == ["drop"]
    finally:
        await client.close()


async def test_detail_unknown_id_returns_404(capture_store):
    app = _admin_app()
    client = await _client(app)
    try:
        resp = await client.get("/admin/captures/does-not-exist", headers=_HEADERS)
        assert resp.status == 404
        data = await resp.json()
        assert data["error"]["code"] == "capture_not_found"
    finally:
        await client.close()


async def test_detail_renders_bodies_for_json(capture_store):
    capture_id = _seed(
        capture_store,
        request_id="req-a",
        provider="openrouter",
        model="m1",
        pool="code",
        reason="qualification_failed",
        request=b'{"prompt":"hello"}',
        response=b'{"ok":true}',
    )
    app = _admin_app()
    client = await _client(app)
    try:
        resp = await client.get(f"/admin/captures/{capture_id}", headers=_HEADERS)
        assert resp.status == 200
        data = await resp.json()
        assert data["capture_id"] == capture_id
        assert data["request_text"] == '{"prompt":"hello"}'
        assert data["request_base64"] == base64.b64encode(b'{"prompt":"hello"}').decode()
        assert data["response_text"] == '{"ok":true}'
        assert "request_body" not in data
    finally:
        await client.close()


async def test_list_limit_is_clamped(capture_store):
    for index in range(3):
        _seed(
            capture_store,
            request_id=f"r{index}",
            provider="openrouter",
            model="m1",
            pool="code",
            reason="qualification_failed",
        )
    app = _admin_app()
    client = await _client(app)
    try:
        resp = await client.get("/admin/captures?limit=0", headers=_HEADERS)
        data = await resp.json()
        assert data["count"] == 1  # clamped up to the floor

        resp = await client.get("/admin/captures?limit=2", headers=_HEADERS)
        data = await resp.json()
        assert data["count"] == 2

        resp = await client.get("/admin/captures?limit=9999", headers=_HEADERS)
        data = await resp.json()
        assert data["count"] == 3  # ceiling clamp still returns all seeded rows
    finally:
        await client.close()


async def test_privacy_pool_capture_withholds_bodies(capture_store):
    capture_id = _seed(
        capture_store,
        request_id="priv",
        provider="openrouter",
        model="m1",
        pool="privacy",
        reason="qualification_failed",
        request=b'{"prompt":"private-secret"}',
    )
    app = _admin_app()
    client = await _client(app)
    try:
        resp = await client.get("/admin/captures", headers=_HEADERS)
        data = await resp.json()
        listed = [c for c in data["captures"] if c["capture_id"] == capture_id]
        assert len(listed) == 1
        assert listed[0]["body_policy"] == "excluded_pool"

        resp = await client.get(f"/admin/captures/{capture_id}", headers=_HEADERS)
        assert resp.status == 200
        text = await resp.text()
        assert "private-secret" not in text
        detail = json.loads(text)
        assert detail["body_policy"] == "excluded_pool"
        assert detail["request_text"] is None
        assert detail["response_text"] is None
        assert detail["replayable"] is False
    finally:
        await client.close()