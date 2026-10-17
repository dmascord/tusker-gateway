"""Access control for the protocol admin surface.

The probe endpoint is the only admin route that can cause billable upstream
calls, so its authorization must be asserted rather than assumed: it is
``admin:write`` (not ``admin:read``), it is closed when the operator gate is
off, and it refuses to probe anything outside the configured endpoint set.

These tests drive the real handler + the real admin access middleware over
HTTP. ``run_probe`` is not mocked: the tests that are expected to reach it
use an unconfigured provider, which the service rejects before any network
call, so the assertions stay hermetic.
"""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.admin import (
    admin_protocol_evidence,
    admin_protocol_probe,
    attach_admin_access_middleware,
)
from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.errors import GatewayError, openai_error
from tusker_gateway.identity import (
    IdentityStore,
    attach_authorization_middleware,
    fingerprint_api_key,
    load_identity_config_from_env,
)

_READ_KEY = "sk-protocol-read"
_WRITE_KEY = "sk-protocol-write"


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


def _identities(scopes: list[str]):
    return load_identity_config_from_env({
        "TUSKER_IDENTITIES_JSON": json.dumps({
            fingerprint_api_key(_READ_KEY if "admin:write" not in scopes else _WRITE_KEY): {
                "principal": "protocol-test",
                "tenant": "operations",
                "scopes": scopes,
            }
        })
    })


def _app(scopes: list[str]) -> web.Application:
    app = web.Application()
    app["config"] = {
        "api_keys": [_READ_KEY, _WRITE_KEY],
        # Deliberately empty: the probe allowlist resolves against the
        # configured registry, so an unconfigured provider must be refused.
        "providers": {},
        "provider_api_keys": {},
        "excluded_providers": [],
        "disabled_providers": [],
    }
    store = IdentityStore(_identities(scopes))
    app["identity_store"] = store
    app.middlewares.append(_auth_middleware(store))
    attach_authorization_middleware(app)
    attach_admin_access_middleware(app)
    app.router.add_post("/admin/protocol/probe", admin_protocol_probe)
    app.router.add_get("/admin/protocol/evidence", admin_protocol_evidence)
    return app


async def _client(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_probe_route_rejects_missing_credentials():
    app = _app(["admin:read", "admin:write"])
    client = await _client(app)
    try:
        resp = await client.post("/admin/protocol/probe", json={"approved": True})
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_probe_route_rejects_read_only_scope():
    """Probing spends money; ``admin:read`` must not be enough.

    This is the negative access test the runbook's exit criteria require:
    the write scope is enforced by the middleware for POST, before the
    handler can reach the operator gate or the provider registry.
    """
    app = _app(["admin:read"])
    client = await _client(app)
    try:
        resp = await client.post(
            "/admin/protocol/probe",
            json={"provider": "openrouter", "model": "x", "approved": True},
            headers={"Authorization": f"Bearer {_READ_KEY}"},
        )
        assert resp.status == 403
        data = await resp.json()
        assert data["error"]["code"] == "insufficient_scope"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_evidence_route_allows_read_scope():
    """The evidence dump is read-only and must stay reachable for operators."""
    app = _app(["admin:read"])
    client = await _client(app)
    try:
        resp = await client.get(
            "/admin/protocol/evidence",
            headers={"Authorization": f"Bearer {_READ_KEY}"},
        )
        assert resp.status == 200
        data = await resp.json()
        # No payload/credential material in the operator view.
        assert "evidence" in data
        assert "probe_version" in data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_probe_gate_closed_by_default(monkeypatch):
    """With the operator gate off, an authorized probe still refuses.

    Authorization is necessary but not sufficient: ``TUSKER_PROTOCOL_PROBES_ENABLED``
    is the operator's second control, so an admin key alone cannot start
    spending.
    """
    from tusker_gateway.protocol_probe_service import ProtocolProbeError, run_probe

    monkeypatch.delenv("TUSKER_PROTOCOL_PROBES_ENABLED", raising=False)
    with pytest.raises(ProtocolProbeError) as excinfo:
        await run_probe({"config": {"providers": {}}}, {
            "provider": "openrouter", "model": "x", "approved": True,
        })
    assert excinfo.value.status == 403


@pytest.mark.asyncio
async def test_probe_refuses_unconfigured_provider(monkeypatch):
    """The allowlist is the configured registry, not client input.

    With the operator gate enabled, a provider that is not in the registry
    (here a link-local metadata address, the classic SSRF target) must be
    refused before any request is made.
    """
    from tusker_gateway.protocol_probe_service import ProtocolProbeError, run_probe

    # The operator gate is a separate control (asserted above); enable it so
    # the allowlist is the behaviour under test, and pass an SSRF-shaped
    # provider name: it must be refused because it is not a configured
    # provider, not because the URL was inspected.
    monkeypatch.setenv("TUSKER_PROTOCOL_PROBES_ENABLED", "1")
    with pytest.raises(ProtocolProbeError) as excinfo:
        await run_probe({"config": {"providers": {}}}, {
            "provider": "http://169.254.169.254", "model": "x", "approved": True,
        })
    assert "unconfigured provider" in str(excinfo.value)
    assert excinfo.value.status == 404


@pytest.mark.asyncio
async def test_probe_write_scope_passes_middleware_then_service_refuses(
    monkeypatch,
):
    """A write-scoped key clears the middleware and reaches the service.

    This is the counterpart to the read-only 403: it proves that refusal
    came from *authorization*, not from the service failing for every
    caller. With the operator gate enabled and an empty registry the
    handler answers 404 ``unknown_provider`` — i.e. past the middleware.
    """
    monkeypatch.setenv("TUSKER_PROTOCOL_PROBES_ENABLED", "1")
    app = _app(["admin:read", "admin:write"])
    client = await _client(app)
    try:
        resp = await client.post(
            "/admin/protocol/probe",
            json={"provider": "openrouter", "model": "x", "approved": True},
            headers={"Authorization": f"Bearer {_WRITE_KEY}"},
        )
        # 404 unknown_provider (registry empty) — not 401/403.
        assert resp.status == 404
        data = await resp.json()
        assert data["error"]["code"] == "unknown_provider"
    finally:
        await client.close()
