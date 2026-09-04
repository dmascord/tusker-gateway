"""Deterministic coverage for enterprise identity, audit, and resilience controls."""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.audit import AuditConfig, AuditLogger, attach_audit_middleware
from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.deadline import DeadlineConfig, attach_deadline_middleware
from tusker_gateway.errors import GatewayError, openai_error
from tusker_gateway.idempotency import (
    IdempotencyConfig,
    IdempotencyStore,
    attach_idempotency_middleware,
)
from tusker_gateway.identity import (
    IdentityStore,
    attach_authorization_middleware,
    fingerprint_api_key,
    load_identity_config_from_env,
)
from tusker_gateway.observability import attach_request_id_middleware


def _auth_middleware(store: IdentityStore):
    auth = AuthMiddleware(store)

    @web.middleware
    async def middleware(request, handler):
        try:
            await auth.verify(request)
        except GatewayError as exc:
            return web.json_response(
                openai_error(exc.message, code=exc.code, error_type=exc.error_type),
                status=exc.status,
            )
        return await handler(request)

    return middleware


async def _client(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


class TestEnterpriseIdentity:
    def test_loads_fingerprint_keyed_policy(self):
        fingerprint = fingerprint_api_key("sk-enterprise")
        config = load_identity_config_from_env(
            {
                "TUSKER_IDENTITY_REQUIRED": "true",
                "TUSKER_IDENTITIES_JSON": json.dumps(
                    {
                        fingerprint: {
                            "principal": "svc-build",
                            "tenant": "engineering",
                            "scopes": ["inference:chat"],
                            "allowed_pools": ["code"],
                        }
                    }
                ),
            }
        )
        identity = config.identities[fingerprint]
        assert identity.principal == "svc-build"
        assert identity.tenant == "engineering"
        assert identity.allows_scope("inference:chat")
        assert not identity.allows_scope("inference:images")

    def test_required_identity_config_fails_closed(self):
        with pytest.raises(ValueError, match="IDENTITIES_JSON is empty"):
            load_identity_config_from_env({"TUSKER_IDENTITY_REQUIRED": "true"})

    @pytest.mark.asyncio
    async def test_scope_and_pool_policy_are_enforced(self):
        api_key = "sk-enterprise"
        fingerprint = fingerprint_api_key(api_key)
        config = load_identity_config_from_env(
            {
                "TUSKER_IDENTITY_REQUIRED": "true",
                "TUSKER_IDENTITIES_JSON": json.dumps(
                    {
                        fingerprint: {
                            "principal": "svc-build",
                            "tenant": "engineering",
                            "scopes": ["inference:chat"],
                            "allowed_pools": ["code"],
                            "allowed_models": ["hermes-code", "openrouter/*"],
                            "allowed_providers": ["openrouter"],
                        }
                    }
                ),
            }
        )
        app = web.Application()
        app["config"] = {"api_keys": [api_key]}
        app.middlewares.append(_auth_middleware(IdentityStore(config)))
        attach_authorization_middleware(app)

        async def handler(request):
            identity = request["identity"]
            body = await request.json()
            return web.json_response(
                {"principal": identity.principal, "model": body.get("model")}
            )

        app.router.add_post("/v1/chat/completions", handler)
        app.router.add_post("/v1/images/generations", handler)
        client = await _client(app)
        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            allowed = await client.post(
                "/v1/chat/completions",
                headers=headers,
                json={"model": "hermes-code", "messages": []},
            )
            assert allowed.status == 200
            assert (await allowed.json())["principal"] == "svc-build"

            pool_denied = await client.post(
                "/v1/chat/completions",
                headers=headers,
                json={"model": "hermes-premium", "messages": []},
            )
            assert pool_denied.status == 403
            assert (await pool_denied.json())["error"]["code"] == "pool_not_allowed"

            scope_denied = await client.post(
                "/v1/images/generations",
                headers=headers,
                json={"model": "openrouter/image-model", "prompt": "safe"},
            )
            assert scope_denied.status == 403
            assert (await scope_denied.json())["error"]["code"] == "insufficient_scope"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_anthropic_key_receives_identity(self):
        api_key = "sk-anthropic"
        config = load_identity_config_from_env(
            {
                "TUSKER_IDENTITIES_JSON": json.dumps(
                    {
                        fingerprint_api_key(api_key): {
                            "principal": "claude-client",
                            "tenant": "research",
                        }
                    }
                )
            }
        )
        app = web.Application()
        app["config"] = {"api_keys": [api_key]}
        app.middlewares.append(_auth_middleware(IdentityStore(config)))

        async def handler(request):
            return web.json_response(
                {"fingerprint": request["_api_key_fingerprint"]}
            )

        app.router.add_get("/identity", handler)
        client = await _client(app)
        try:
            response = await client.get("/identity", headers={"x-api-key": api_key})
            assert response.status == 200
            assert (await response.json())["fingerprint"] == fingerprint_api_key(api_key)
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_restrictive_policy_fails_closed_for_uninspectable_body(self):
        api_key = "sk-multipart"
        config = load_identity_config_from_env(
            {
                "TUSKER_IDENTITIES_JSON": json.dumps(
                    {
                        fingerprint_api_key(api_key): {
                            "principal": "image-editor",
                            "tenant": "creative",
                            "scopes": ["inference:images"],
                            "allowed_pools": ["media"],
                            "allowed_models": ["gpt-image-*"],
                        }
                    }
                )
            }
        )
        app = web.Application()
        app["config"] = {"api_keys": [api_key]}
        app.middlewares.append(_auth_middleware(IdentityStore(config)))
        attach_authorization_middleware(app)
        called = False

        async def handler(request):
            nonlocal called
            called = True
            return web.Response(text="unexpected")

        app.router.add_post("/v1/images/edits", handler)
        client = await _client(app)
        try:
            response = await client.post(
                "/v1/images/edits",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/octet-stream",
                },
                data=b"opaque-image",
            )
            assert response.status == 403
            assert (await response.json())["error"]["code"] == "request_policy_uninspectable"
            assert called is False
        finally:
            await client.close()


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_hash_chain_detects_tampering(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        config = AuditConfig(path=str(path), hmac_key="audit-secret", fsync=False)
        audit = AuditLogger(config)
        await audit.write({"event_type": "gateway.request", "status": 200})
        await audit.write({"event_type": "gateway.request", "status": 403})

        assert AuditLogger.verify_file(config) == (True, 2)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert records[1]["previous_hash"] == records[0]["hash"]

        records[0]["status"] = 500
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        assert AuditLogger.verify_file(config)[0] is False

    @pytest.mark.asyncio
    async def test_middleware_records_identity_without_raw_key(self, tmp_path):
        api_key = "sk-never-write-this"
        fingerprint = fingerprint_api_key(api_key)
        identities = load_identity_config_from_env(
            {
                "TUSKER_IDENTITIES_JSON": json.dumps(
                    {
                        fingerprint: {
                            "principal": "svc-audit",
                            "tenant": "security",
                        }
                    }
                )
            }
        )
        path = tmp_path / "audit.jsonl"
        app = web.Application()
        app["config"] = {"api_keys": [api_key]}
        attach_request_id_middleware(app)
        attach_audit_middleware(
            app,
            AuditLogger(AuditConfig(path=str(path), hmac_key="key", fsync=False)),
        )
        app.middlewares.append(_auth_middleware(IdentityStore(identities)))
        app.router.add_get("/status", lambda request: web.json_response({"ok": True}))
        client = await _client(app)
        try:
            response = await client.get(
                "/status", headers={"Authorization": f"Bearer {api_key}"}
            )
            assert response.status == 200
        finally:
            await client.close()

        content = path.read_text()
        assert api_key not in content
        record = json.loads(content)
        assert record["principal"] == "svc-audit"
        assert record["tenant"] == "security"
        assert record["key_fingerprint"] == fingerprint


class TestRequestDeadlines:
    @pytest.mark.asyncio
    async def test_slow_request_returns_openai_compatible_504(self):
        import asyncio

        app = web.Application()
        attach_deadline_middleware(
            app, DeadlineConfig(default_timeout_ms=10, max_timeout_ms=50)
        )

        async def slow_handler(request):
            await asyncio.sleep(0.05)
            return web.json_response({"late": True})

        app.router.add_post("/v1/chat/completions", slow_handler)
        client = await _client(app)
        try:
            response = await client.post("/v1/chat/completions", json={})
            assert response.status == 504
            body = await response.json()
            assert body["error"]["code"] == "request_timeout"
            assert response.headers["X-Tusker-Timeout-Ms"] == "10"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_client_timeout_is_capped(self):
        app = web.Application()
        attach_deadline_middleware(
            app, DeadlineConfig(default_timeout_ms=100, max_timeout_ms=250)
        )
        app.router.add_post("/v1/chat/completions", lambda request: web.Response(text="ok"))
        client = await _client(app)
        try:
            response = await client.post(
                "/v1/chat/completions", headers={"X-Tusker-Timeout-Ms": "999999"}
            )
            assert response.status == 200
            assert response.headers["X-Tusker-Timeout-Ms"] == "250"
        finally:
            await client.close()


class TestIdempotency:
    def test_store_reserves_replays_and_rejects_mismatched_payload(self, tmp_path):
        store = IdempotencyStore(
            IdempotencyConfig(enabled=True, path=str(tmp_path / "idempotency.db"))
        )
        assert store.claim("record", "hash-a").state == "claimed"
        assert store.claim("record", "hash-a").state == "in_progress"
        assert store.claim("record", "hash-b").state == "conflict"
        store.complete("record", "hash-a", 201, b'{"ok":true}', "application/json")
        replay = store.claim("record", "hash-a")
        assert replay.state == "replay"
        assert replay.status == 201
        assert replay.body == b'{"ok":true}'

    @pytest.mark.asyncio
    async def test_middleware_executes_once_and_replays_response(self, tmp_path):
        store = IdempotencyStore(
            IdempotencyConfig(enabled=True, path=str(tmp_path / "idempotency.db"))
        )
        app = web.Application()
        attach_idempotency_middleware(app, store)
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            body = await request.json()
            return web.json_response({"call": calls, "model": body["model"]})

        app.router.add_post("/v1/chat/completions", handler)
        client = await _client(app)
        try:
            headers = {
                "Authorization": "Bearer sk-client",
                "Idempotency-Key": "request-42",
            }
            first = await client.post(
                "/v1/chat/completions", headers=headers, json={"model": "hermes-code"}
            )
            second = await client.post(
                "/v1/chat/completions", headers=headers, json={"model": "hermes-code"}
            )
            conflict = await client.post(
                "/v1/chat/completions", headers=headers, json={"model": "hermes-premium"}
            )

            assert first.status == 200
            assert first.headers["Idempotency-Replayed"] == "false"
            assert second.status == 200
            assert second.headers["Idempotency-Replayed"] == "true"
            assert await second.json() == {"call": 1, "model": "hermes-code"}
            assert conflict.status == 409
            assert (await conflict.json())["error"]["code"] == "idempotency_conflict"
            assert calls == 1
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_streaming_requests_are_never_replayed(self, tmp_path):
        store = IdempotencyStore(
            IdempotencyConfig(enabled=True, path=str(tmp_path / "idempotency.db"))
        )
        app = web.Application()
        attach_idempotency_middleware(app, store)
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            return web.json_response({"call": calls})

        app.router.add_post("/v1/chat/completions", handler)
        client = await _client(app)
        try:
            headers = {"Idempotency-Key": "stream-1"}
            one = await client.post(
                "/v1/chat/completions", headers=headers, json={"stream": True}
            )
            two = await client.post(
                "/v1/chat/completions", headers=headers, json={"stream": True}
            )
            assert (await one.json())["call"] == 1
            assert (await two.json())["call"] == 2
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_non_json_body_is_not_consumed(self, tmp_path):
        store = IdempotencyStore(
            IdempotencyConfig(enabled=True, path=str(tmp_path / "idempotency.db"))
        )
        app = web.Application()
        attach_idempotency_middleware(app, store)
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            return web.Response(body=await request.read())

        app.router.add_post("/v1/images/edits", handler)
        client = await _client(app)
        try:
            headers = {
                "Idempotency-Key": "multipart-1",
                "Content-Type": "application/octet-stream",
            }
            one = await client.post("/v1/images/edits", headers=headers, data=b"image-one")
            two = await client.post("/v1/images/edits", headers=headers, data=b"image-two")
            assert await one.read() == b"image-one"
            assert await two.read() == b"image-two"
            assert calls == 2
        finally:
            await client.close()
