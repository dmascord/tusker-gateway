"""Deterministic coverage for the read-only admin API."""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.admin import (
    admin_breakers,
    admin_catalog,
    admin_cooldowns,
    admin_diagnostics,
    admin_keys,
    admin_login,
    admin_logout,
    admin_page,
    admin_pools,
    admin_providers,
    admin_session,
    admin_usage,
    attach_admin_access_middleware,
    register_admin_config_routes,
)
from tusker_gateway.admin import SessionManager, _login_limiter
from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
from tusker_gateway.errors import GatewayError, openai_error
from tusker_gateway.identity import (
    IdentityStore,
    attach_authorization_middleware,
    fingerprint_api_key,
    load_identity_config_from_env,
)


def _auth_middleware(store: IdentityStore | None = None):
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


_ADMIN_IDENTITY_KEY = "sk-admin-test"


def _admin_identities():
    fingerprint = fingerprint_api_key(_ADMIN_IDENTITY_KEY)
    return load_identity_config_from_env({
        "TUSKER_IDENTITIES_JSON": json.dumps({
            fingerprint: {
                "principal": "admin-test",
                "tenant": "operations",
                "scopes": ["admin:read"],
            }
        })
    })


async def _client(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _admin_app(api_key: str, identities=None):
    app = web.Application()
    app["config"] = {
        "api_keys": [api_key],
        "providers": DEFAULT_PROVIDER_REGISTRY,
        "provider_api_keys": {"openrouter": "sk-test-openrouter-secret"},
        "excluded_providers": [],
        "disabled_providers": [],
    }
    identity_cfg = (
        identities
        if identities is not None
        else load_identity_config_from_env({})
    )
    store = IdentityStore(identity_cfg)
    app["identity_store"] = store
    app.middlewares.append(_auth_middleware(store))
    attach_authorization_middleware(app)
    attach_admin_access_middleware(app)
    app.router.add_get("/admin", admin_page)
    app.router.add_get("/admin/", admin_page)
    app.router.add_post("/admin/login", admin_login)
    app.router.add_post("/admin/logout", admin_logout)
    app.router.add_get("/admin/session", admin_session)
    app.router.add_get("/admin/diagnostics", admin_diagnostics)
    app.router.add_get("/admin/providers", admin_providers)
    app.router.add_get("/admin/pools", admin_pools)
    app.router.add_get("/admin/catalog", admin_catalog)
    app.router.add_get("/admin/cooldowns", admin_cooldowns)
    app.router.add_get("/admin/breakers", admin_breakers)
    app.router.add_get("/admin/keys", admin_keys)
    app.router.add_get("/admin/usage", admin_usage)
    register_admin_config_routes(app)
    return app

ADMIN_PATHS = [
    "/admin/diagnostics",
    "/admin/providers",
    "/admin/pools",
    "/admin/catalog",
    "/admin/cooldowns",
    "/admin/breakers",
    "/admin/keys",
    "/admin/usage",
]


@pytest.mark.asyncio
async def test_admin_routes_require_auth():
    app = _admin_app("sk-admin-test")
    client = await _client(app)
    try:
        for path in ADMIN_PATHS:
            resp = await client.get(path)
            assert resp.status == 401, path
            data = await resp.json()
            assert "error" in data, path
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_rejects_wrong_key():
    app = _admin_app("sk-admin-test")
    client = await _client(app)
    try:
        resp = await client.get(
            "/admin/diagnostics", headers={"Authorization": "Bearer not-the-key"}
        )
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_legacy_key_denied_without_admin_profile():
    """A valid key with no identity profile is rejected — admin console needs a dedicated key."""
    app = _admin_app("sk-admin-test")
    client = await _client(app)
    try:
        for path in ADMIN_PATHS:
            resp = await client.get(
                path, headers={"Authorization": "Bearer sk-admin-test"}
            )
            assert resp.status == 403, path
        # Login is also blocked for unscoped keys
        resp = await client.post("/admin/login", json={"api_key": "sk-admin-test"})
        assert resp.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_scoped_identity_without_admin_scope_denied():
    fingerprint = fingerprint_api_key("sk-scoped")
    identities = load_identity_config_from_env(
        {
            "TUSKER_IDENTITIES_JSON": json.dumps(
                {
                    fingerprint: {
                        "principal": "svc-app",
                        "tenant": "engineering",
                        "scopes": ["inference:chat", "models:read"],
                    }
                }
            )
        }
    )
    app = _admin_app("sk-scoped", identities=identities)
    client = await _client(app)
    try:
        resp = await client.get(
            "/admin/diagnostics", headers={"Authorization": "Bearer sk-scoped"}
        )
        assert resp.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_scoped_identity_with_admin_scope_allowed():
    fingerprint = fingerprint_api_key("sk-ops")
    identities = load_identity_config_from_env(
        {
            "TUSKER_IDENTITIES_JSON": json.dumps(
                {
                    fingerprint: {
                        "principal": "svc-ops",
                        "tenant": "operations",
                        "scopes": ["admin:read"],
                    }
                }
            )
        }
    )
    app = _admin_app("sk-ops", identities=identities)
    client = await _client(app)
    try:
        for path in ADMIN_PATHS:
            resp = await client.get(
                path, headers={"Authorization": "Bearer sk-ops"}
            )
            assert resp.status == 200, path
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_providers_never_returns_raw_keys():
    app = _admin_app("sk-admin-test", identities=_admin_identities())
    client = await _client(app)
    try:
        resp = await client.get(
            "/admin/providers", headers={"Authorization": "Bearer sk-admin-test"}
        )
        assert resp.status == 200
        body = await resp.text()
        assert "sk-test-openrouter-secret" not in body
        assert "sk-admin-test" not in body
        data = json.loads(body)
        openrouter = data["providers"]["openrouter"]
        assert openrouter["has_key"] is True
        assert openrouter["key_fingerprint"]
        assert openrouter["base_url"].startswith("https://openrouter.ai")
        assert openrouter["auth_kind"] == "bearer"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_keys_reports_identity_metadata_not_raw_keys():
    fingerprint = fingerprint_api_key("sk-ops")
    identities = load_identity_config_from_env(
        {
            "TUSKER_IDENTITIES_JSON": json.dumps(
                {
                    fingerprint: {
                        "principal": "svc-ops",
                        "tenant": "operations",
                        "scopes": ["admin:read"],
                        "allowed_pools": ["code"],
                    }
                }
            )
        }
    )
    app = _admin_app("sk-ops", identities=identities)
    client = await _client(app)
    try:
        resp = await client.get(
            "/admin/keys", headers={"Authorization": "Bearer sk-ops"}
        )
        assert resp.status == 200
        body = await resp.text()
        assert "sk-ops" not in body
        data = json.loads(body)
        assert data["total"] == 1
        entry = data["keys"][0]
        assert entry["fingerprint"] == fingerprint
        assert entry["principal"] == "svc-ops"
        assert entry["tenant"] == "operations"
        assert entry["allowed_pools"] == ["code"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_diagnostics_aggregates_subsystems():
    app = _admin_app("sk-admin-test", identities=_admin_identities())
    client = await _client(app)
    try:
        resp = await client.get(
            "/admin/diagnostics", headers={"Authorization": "Bearer sk-admin-test"}
        )
        assert resp.status == 200
        data = await resp.json()
        for section in (
            "pools",
            "catalog",
            "quality",
            "provider_usage",
            "cooldowns",
            "circuit_breakers",
            "state_store",
        ):
            assert section in data, section
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_console_page_served():
    app = _admin_app("sk-admin-test")
    client = await _client(app)
    try:
        resp = await client.get("/admin/")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        body = await resp.text()
        assert "Tusker Gateway Admin" in body
        # The SPA must never inline a key.
        assert "sk-admin-test" not in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_login_sets_session_cookie_and_grants_data_routes():
    app = _admin_app("sk-admin-test", identities=_admin_identities())
    client = await _client(app)
    try:
        resp = await client.post(
            "/admin/login", json={"api_key": "sk-admin-test"}
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert "sk-admin-test" not in await resp.text()
        set_cookie = resp.headers.get("Set-Cookie", "")
        assert "HttpOnly" in set_cookie
        assert "Path=/admin" in set_cookie
        assert "tusker_admin_session=" in set_cookie

        # Cookie-authenticated data access (no Bearer header).
        resp = await client.get("/admin/providers")
        assert resp.status == 200
        # Data routes with a cookie never leak the raw key either.
        assert "sk-admin-test" not in await resp.text()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_login_rejects_wrong_key_and_non_admin_scope():
    # Wrong key -> 401
    app = _admin_app("sk-admin-test")
    client = await _client(app)
    try:
        resp = await client.post("/admin/login", json={"api_key": "nope"})
        assert resp.status == 401

        # Scoped key without admin:read -> 403
        scoped_fp = fingerprint_api_key("sk-app-key")
        scoped = load_identity_config_from_env(
            {
                "TUSKER_IDENTITIES_JSON": json.dumps(
                    {
                        scoped_fp: {
                            "principal": "svc-app",
                            "tenant": "engineering",
                            "scopes": ["inference:chat"],
                        }
                    }
                )
            }
        )
        app2 = _admin_app("sk-app-key", identities=scoped)
        client2 = await _client(app2)
        try:
            resp = await client2.post("/admin/login", json={"api_key": "sk-app-key"})
            assert resp.status == 403
        finally:
            await client2.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_logout_revokes_session():
    app = _admin_app("sk-admin-test", identities=_admin_identities())
    client = await _client(app)
    try:
        await client.post("/admin/login", json={"api_key": "sk-admin-test"})
        resp = await client.post("/admin/logout")
        assert resp.status == 200
        # The revoked cookie must no longer grant access.
        resp = await client.get("/admin/providers")
        assert resp.status == 401
    finally:
        await client.close()


def test_session_rejects_forged_and_expired_cookies():
    manager = SessionManager(["sk-admin-test"])
    cookie, _ = manager.issue()
    assert manager.validate(cookie) is not None
    # Tampered signature
    sid, expiry, sig = cookie.split(".")
    assert manager.validate(f"{sid}.{expiry}.{'0' * 64}") is None
    # Wrong signing key
    other = SessionManager(["another-key"])
    assert other.validate(cookie) is None
    # Expired
    short = SessionManager(["sk-admin-test"], ttl_secs=-1)
    expired_cookie, _ = short.issue()
    assert short.validate(expired_cookie) is None
    # Restart scenario: fresh manager doesn't honor another manager's SID.
    fresh = SessionManager(["sk-admin-test"])
    assert fresh.validate(cookie) is None


@pytest.mark.asyncio
async def test_login_rate_limited_after_repeated_failures():
    _login_limiter.reset()
    app = _admin_app("sk-admin-test")
    client = await _client(app)
    try:
        statuses = []
        for _ in range(7):
            resp = await client.post("/admin/login", json={"api_key": "nope"})
            statuses.append(resp.status)
        assert statuses[:5] == [401] * 5
        assert 429 in statuses
    finally:
        _login_limiter.reset()
        await client.close()


@pytest.mark.asyncio
async def test_bearer_still_works_alongside_sessions():
    app = _admin_app("sk-admin-test", identities=_admin_identities())
    client = await _client(app)
    try:
        resp = await client.get(
            "/admin/providers", headers={"Authorization": "Bearer sk-admin-test"}
        )
        assert resp.status == 200
    finally:
        await client.close()
