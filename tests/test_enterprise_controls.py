"""Deterministic coverage for enterprise identity, audit, and resilience controls."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.audit import AuditConfig, AuditLogger, attach_audit_middleware
from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.deadline import DeadlineConfig, attach_deadline_middleware
from tusker_gateway.endpoints import (
    _call_with_pool_fallback,
    images_handler,
    tts_handler,
    video_handler,
)
from tusker_gateway.errors import GatewayError, NoHealthyModelsError, openai_error
from tusker_gateway.idempotency import (
    IdempotencyConfig,
    IdempotencyStore,
    attach_idempotency_middleware,
)
from tusker_gateway.identity import (
    IdentityStore,
    attach_authorization_middleware,
    authorize_request_body,
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("allowed_pools", "expected_status", "expected_selected_pools"),
        [
            (("code",), 503, ["code", "code"]),
            (("code", "premium"), 200, ["code", "premium"]),
        ],
    )
    async def test_pool_fallback_respects_identity_pool_policy(
        self,
        allowed_pools,
        expected_status,
        expected_selected_pools,
    ):
        api_key = "sk-pool-policy"
        fingerprint = fingerprint_api_key(api_key)
        config = load_identity_config_from_env(
            {
                "TUSKER_IDENTITY_REQUIRED": "true",
                "TUSKER_IDENTITIES_JSON": json.dumps(
                    {
                        fingerprint: {
                            "principal": "svc-pool-policy",
                            "tenant": "engineering",
                            "scopes": ["inference:chat"],
                            "allowed_pools": list(allowed_pools),
                        }
                    }
                ),
            }
        )
        app = web.Application()
        app["config"] = {"api_keys": [api_key]}
        pool_manager = MagicMock()
        pool_manager.fallback_pools.return_value = ("premium",)
        if "premium" in allowed_pools:
            pool_manager.select.side_effect = [None, ("openai-codex", "premium-model")]
        else:
            pool_manager.select.return_value = None
        app["pool_manager"] = pool_manager
        app.middlewares.append(_auth_middleware(IdentityStore(config)))
        attach_authorization_middleware(app)

        class FakeClient:
            calls = 0

            async def chat(self, provider, model, messages, **kwargs):
                self.calls += 1
                return {"provider": provider, "model": model}

        upstream = FakeClient()

        async def handler(request):
            body = await request.json()
            try:
                provider, model, _ = await _call_with_pool_fallback(
                    app["config"], body, upstream, request=request
                )
            except NoHealthyModelsError:
                return web.json_response({"error": "no route"}, status=503)
            return web.json_response({"provider": provider, "model": model})

        app.router.add_post("/v1/chat/completions", handler)
        client = await _client(app)
        try:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "hermes-code",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            assert response.status == expected_status
            assert [call.args[0] for call in pool_manager.select.call_args_list] == (
                expected_selected_pools
            )
            assert upstream.calls == (1 if expected_status == 200 else 0)
        finally:
            await client.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("rewritten_model", "expected_status"),
        [("hermes-code", 200), ("hermes-premium", 403)],
    )
    async def test_body_rewrite_is_reauthorized(
        self, rewritten_model, expected_status
    ):
        api_key = "sk-rewrite-policy"
        config = load_identity_config_from_env(
            {
                "TUSKER_IDENTITIES_JSON": json.dumps(
                    {
                        fingerprint_api_key(api_key): {
                            "principal": "svc-rewrite",
                            "tenant": "engineering",
                            "scopes": ["inference:chat"],
                            "allowed_pools": ["code"],
                        }
                    }
                )
            }
        )
        app = web.Application()
        app["config"] = {"api_keys": [api_key]}
        app.middlewares.append(_auth_middleware(IdentityStore(config)))
        attach_authorization_middleware(app)

        async def handler(request):
            await request.json()
            try:
                authorize_request_body(request, {"model": rewritten_model})
            except GatewayError as exc:
                return web.json_response(
                    openai_error(
                        exc.message, code=exc.code, error_type=exc.error_type
                    ),
                    status=exc.status,
                )
            return web.json_response({"model": rewritten_model})

        app.router.add_post("/v1/chat/completions", handler)
        client = await _client(app)
        try:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "hermes-code"},
            )
            assert response.status == expected_status
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

    @pytest.mark.asyncio
    async def test_oversized_metadata_is_bounded_and_chain_remains_appendable(
        self, tmp_path
    ):
        path = tmp_path / "audit.jsonl"
        config = AuditConfig(
            path=str(path), hmac_key="audit-secret", fail_closed=True, fsync=False
        )
        audit = AuditLogger(config)

        await audit.write({"event_type": "gateway.request", "model": "m" * 70_000})
        await audit.write({"event_type": "gateway.request", "model": "normal"})

        assert AuditLogger.verify_file(config) == (True, 2)
        first = json.loads(path.read_text().splitlines()[0])
        assert len(first["model"]) <= 2_048


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
    async def test_query_parameters_participate_in_request_identity(self, tmp_path):
        store = IdempotencyStore(
            IdempotencyConfig(enabled=True, path=str(tmp_path / "idempotency.db"))
        )
        app = web.Application()
        attach_idempotency_middleware(app, store)

        async def handler(request):
            return web.json_response(
                {
                    "wait": request.query.get("wait"),
                    "quality": request.query.get("quality"),
                }
            )

        app.router.add_post("/v1/videos", handler)
        client = await _client(app)
        try:
            headers = {"Idempotency-Key": "video-42"}
            first = await client.post(
                "/v1/videos?wait=false&quality=high",
                headers=headers,
                json={"model": "sora-2"},
            )
            reordered = await client.post(
                "/v1/videos?quality=high&wait=false",
                headers=headers,
                json={"model": "sora-2"},
            )
            changed = await client.post(
                "/v1/videos?wait=true&quality=high",
                headers=headers,
                json={"model": "sora-2"},
            )

            assert first.status == 200
            assert reordered.status == 200
            assert reordered.headers["Idempotency-Replayed"] == "true"
            assert changed.status == 409
            assert (await changed.json())["error"]["code"] == "idempotency_conflict"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_deadline_cancellation_releases_processing_claim(self, tmp_path):
        store = IdempotencyStore(
            IdempotencyConfig(
                enabled=True,
                path=str(tmp_path / "idempotency.db"),
                lock_secs=30,
            )
        )
        app = web.Application()
        attach_deadline_middleware(
            app, DeadlineConfig(default_timeout_ms=10, max_timeout_ms=10)
        )
        attach_idempotency_middleware(app, store)
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(0.05)
            return web.json_response({"call": calls})

        app.router.add_post("/v1/chat/completions", handler)
        client = await _client(app)
        try:
            headers = {"Idempotency-Key": "cancelled-operation"}
            first = await client.post(
                "/v1/chat/completions", headers=headers, json={"model": "hermes-code"}
            )
            second = await client.post(
                "/v1/chat/completions", headers=headers, json={"model": "hermes-code"}
            )

            assert first.status == 504
            assert second.status == 200
            assert await second.json() == {"call": 2}
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


class _FakeImageHandler:
    def get_provider_for_image_request(self, model, path):
        return "openai"

    async def handle_request(self, **kwargs):
        return {"data": []}


class _FakeTTSHandler:
    called = False

    def get_provider_for_tts_request(self, model):
        return "openai"

    async def handle_request(self, **kwargs):
        self.called = True
        return b"audio", "audio/mpeg"


class _FakeVideoHandler:
    def get_provider_for_video_request(self, model):
        return "openai"

    async def handle_request(self, **kwargs):
        return {"id": "video-1"}


class TestMediaEnterpriseControls:
    @staticmethod
    async def _media_client(path, handler_name, handler, *, allowed):
        class RateLimiter:
            def check(self, api_key):
                return SimpleNamespace(
                    allowed=allowed,
                    retry_after=60,
                    reason=None if allowed else "blocked by policy",
                )

        class CaptureLog:
            fields = None

            def log(self, request, response_status, latency_ms, **fields):
                self.fields = fields

        app = web.Application()
        capture = CaptureLog()
        app["config"] = {"provider_api_keys": {"openai": "upstream-key"}}
        app[handler_name] = handler
        app["ratelimit"] = RateLimiter()
        app["budget"] = None
        app["guard_pipeline"] = None
        app["model_capabilities"] = None
        app["access_log"] = capture
        attach_request_id_middleware(app)
        route_handler = {
            "image_handler": images_handler,
            "tts_handler": tts_handler,
            "video_handler": video_handler,
        }[handler_name]
        app.router.add_post(path, route_handler)
        client = await _client(app)
        return client, capture

    @pytest.mark.asyncio
    async def test_tts_rate_limit_blocks_provider_dispatch(self):
        tts = _FakeTTSHandler()
        client, _ = await self._media_client(
            "/v1/audio/speech", "tts_handler", tts, allowed=False
        )
        try:
            response = await client.post(
                "/v1/audio/speech",
                headers={"Authorization": "Bearer sk-client"},
                json={"model": "tts-1", "input": "hello"},
            )
            assert response.status == 429
            assert tts.called is False
        finally:
            await client.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "handler_name", "model", "handler_factory"),
        [
            (
                "/v1/images/generations",
                "image_handler",
                "openai::gpt-image-1",
                _FakeImageHandler,
            ),
            ("/v1/audio/speech", "tts_handler", "tts-1", _FakeTTSHandler),
            ("/v1/videos", "video_handler", "openai::sora-2", _FakeVideoHandler),
        ],
    )
    async def test_successful_media_request_populates_access_routing_fields(
        self, path, handler_name, model, handler_factory
    ):
        client, capture = await self._media_client(
            path, handler_name, handler_factory(), allowed=True
        )
        try:
            response = await client.post(
                path,
                headers={"Authorization": "Bearer sk-client"},
                json={"model": model, "input": "hello", "prompt": "hello"},
            )
            assert response.status == 200
            assert capture.fields["pool"] == "media"
            assert capture.fields["provider"] == "openai"
            assert capture.fields["model"] == model
        finally:
            await client.close()
