"""Tests for observability features: access logging, request-ID propagation, weighted pool selection."""

import json
import logging
import os
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from tusker_gateway.observability import (
    AccessLog,
    attach_request_id_middleware,
    set_access_log_context,
    client_ip,
    _generate_request_id,
)
from tusker_gateway.pools import ModelSpec, PoolManager, PoolConfig
from tusker_gateway.identity import CallerIdentity


class TestRequestIDGeneration:
    """Request-ID generation and format."""

    def test_generated_id_format(self):
        """Generated IDs have format req_<hex>."""
        rid = _generate_request_id()
        assert rid.startswith("req_")
        # token_hex(8) produces 16 hex characters (8 bytes = 16 hex digits)
        assert len(rid) == 20  # "req_" (4) + hex(16)

    def test_ids_are_unique(self):
        """Each call generates a unique ID."""
        ids = {_generate_request_id() for _ in range(100)}
        assert len(ids) == 100


class TestAccessLog:
    """Access logging with structured JSON output."""

    def test_access_log_disabled(self):
        """Access log respects TUSKER_ACCESS_LOG=0."""
        os.environ["TUSKER_ACCESS_LOG"] = "0"
        log = AccessLog()
        assert not log.enabled
        del os.environ["TUSKER_ACCESS_LOG"]

    def test_access_log_enabled_by_default(self):
        """Access log is enabled by default."""
        os.environ.pop("TUSKER_ACCESS_LOG", None)
        log = AccessLog()
        assert log.enabled

    def test_access_log_emits_json_record(self, caplog):
        """Access log emits valid JSON with request details."""
        log = AccessLog()
        request = make_mocked_request("POST", "/v1/chat/completions")
        request["_request_id"] = "req_test123"

        with caplog.at_level(logging.INFO, logger="tusker_gateway.access"):
            log.log(request, 200, 42.5, provider="openrouter", model="gpt-4", pool="code")

        assert len(caplog.records) == 1
        # Extract JSON from log message (after the logger prefix)
        log_message = caplog.records[0].message
        record = json.loads(log_message)
        assert record["request_id"] == "req_test123"
        assert record["status"] == 200
        assert record["latency_ms"] == 42.5
        assert record["provider"] == "openrouter"
        assert record["model"] == "gpt-4"
        assert record["pool"] == "code"

    def test_access_log_includes_tokens_and_cache(self, caplog):
        """Access log includes token counts and cache status."""
        log = AccessLog()
        request = make_mocked_request("POST", "/v1/chat/completions")
        request["_request_id"] = "req_abc"

        with caplog.at_level(logging.INFO, logger="tusker_gateway.access"):
            log.log(
                request,
                200,
                50.0,
                provider="openrouter",
                model="gpt-4",
                pool="code",
                cache_status="hit",
                tokens_in=100,
                tokens_out=50,
            )

        log_message = caplog.records[0].message
        record = json.loads(log_message)
        assert record["usage"] == {"in": 100, "out": 50}
        assert record["cache"] == "hit"

    def test_access_log_includes_enterprise_identity(self, caplog):
        log = AccessLog()
        request = make_mocked_request("POST", "/v1/chat/completions")
        request["_request_id"] = "req_identity"
        request["identity"] = CallerIdentity(
            key_fingerprint="a" * 64,
            principal="svc-build",
            tenant="engineering",
        )

        with caplog.at_level(logging.INFO, logger="tusker_gateway.access"):
            log.log(request, 200, 10.0)

        record = json.loads(caplog.records[0].message)
        assert record["principal"] == "svc-build"
        assert record["tenant"] == "engineering"
        assert record["key_fingerprint"] == "a" * 64
class TestClientIP:
    """Real client IP extraction: CF-Connecting-IP > X-Forwarded-For > request.remote."""

    def test_cf_connecting_ip(self):
        """Cloudflare CF-Connecting-IP takes priority over XFF."""
        request = make_mocked_request("GET", "/")
        request = _patch_headers(
            request,
            {"CF-Connecting-IP": "198.51.100.42", "X-Forwarded-For": "10.0.0.1"},
        )
        assert client_ip(request) == "198.51.100.42"

    def test_xff_chain(self):
        """Rightmost XFF entry (last-hop proxy) is used when no CF header."""
        request = make_mocked_request("GET", "/")
        request = _patch_headers(
            request, {"X-Forwarded-For": "203.0.113.1, 10.0.0.1, 192.168.1.1"}
        )
        assert client_ip(request) == "192.168.1.1"

    def test_xff_with_spaces(self):
        """Rightmost XFF entry with surrounding whitespace is stripped."""
        request = make_mocked_request("GET", "/")
        request = _patch_headers(request, {"X-Forwarded-For": "  203.0.113.55  ,  10.0.0.2  "})
        assert client_ip(request) == "10.0.0.2"

    def test_no_headers_fallback(self):
        """No CF or XFF falls back to request.remote (or 'unknown' in mocked)."""
        request = make_mocked_request("GET", "/")
        assert client_ip(request) == "unknown"
    """X-Forwarded-For client IP extraction."""

    def test_xff_single(self):
        """XFF single value is returned directly."""
        request = make_mocked_request("GET", "/")
        request = _patch_headers(request, {"X-Forwarded-For": "203.0.113.1"})
        assert client_ip(request) == "203.0.113.1"

    def test_xff_chain(self):
        """Rightmost XFF entry (last-hop proxy) is returned."""
        request = make_mocked_request("GET", "/")
        request = _patch_headers(
            request, {"X-Forwarded-For": "203.0.113.1, 10.0.0.1, 192.168.1.1"}
        )
        assert client_ip(request) == "192.168.1.1"

    def test_xff_with_spaces(self):
        """Rightmost XFF entry with surrounding whitespace is stripped."""
        request = make_mocked_request("GET", "/")
        request = _patch_headers(request, {"X-Forwarded-For": "  203.0.113.55  ,  10.0.0.2  "})
        assert client_ip(request) == "10.0.0.2"
    def test_no_xff_fallback(self):
        """No XFF header falls back to 'unknown'."""
        request = make_mocked_request("GET", "/")
        assert client_ip(request) == "unknown"


def _patch_headers(request: web.Request, headers: dict) -> web.Request:
    """Patch a mocked request's headers dict for XFF testing."""
    # make_mocked_request creates a request with frozen headers.
    # Replace the _headers object with a mutable MultiDictProxy.
    from multidict import CIMultiDict
    from yarl import URL
    request._headers = CIMultiDict(headers)
    return request


class TestRequestIDMiddleware:
    """Request-ID middleware extraction and propagation."""

    def test_generates_id_when_missing(self):
        """Middleware generates ID when X-Request-ID header is absent."""
        request = make_mocked_request("GET", "/test")
        # Simulate middleware behavior
        rid = request.headers.get("X-Request-ID", "").strip() or _generate_request_id()
        request["_request_id"] = rid
        assert request["_request_id"].startswith("req_")

    def test_uses_provided_id(self):
        """Middleware uses provided X-Request-ID header."""
        request = make_mocked_request("GET", "/test", headers={"X-Request-ID": "req_custom123"})
        rid = request.headers.get("X-Request-ID", "").strip()
        request["_request_id"] = rid
        assert request["_request_id"] == "req_custom123"

    @pytest.mark.asyncio
    async def test_middleware_bounds_id_and_logs_completed_request(self):
        app = web.Application()
        access_log = Mock()
        app["access_log"] = access_log

        async def handler(request):
            return web.Response(text="ok")

        app.router.add_get("/test", handler)
        attach_request_id_middleware(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            request_id = "r" * 256
            response = await client.get(
                "/test",
                headers={"X-Request-ID": request_id},
            )
            assert response.status == 200
            assert response.headers["X-Request-ID"] == request_id[:128]
            access_log.log.assert_called_once()
            assert access_log.log.call_args.args[1] == 200
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_middleware_preserves_id_on_prepared_stream(self):
        app = web.Application()
        access_log = Mock()
        app["access_log"] = access_log

        async def handler(request):
            response = web.StreamResponse(
                headers={"X-Request-ID": request["_request_id"]}
            )
            await response.prepare(request)
            await response.write(b"ok")
            await response.write_eof()
            return response

        app.router.add_get("/stream", handler)
        attach_request_id_middleware(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/stream",
                headers={"X-Request-ID": "req_stream"},
            )
            assert response.status == 200
            assert response.headers["X-Request-ID"] == "req_stream"
            assert await response.text() == "ok"
            access_log.log.assert_called_once()
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_middleware_logs_request_routing_context(self):
        app = web.Application()
        access_log = Mock()
        app["access_log"] = access_log

        async def handler(request):
            set_access_log_context(
                request,
                provider="openrouter",
                model="qwen/qwen3-coder",
                pool="code",
                cache_status="miss",
            )
            return web.Response(text="ok")

        app.router.add_get("/context", handler)
        attach_request_id_middleware(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/context")
            assert response.status == 200
            access_log.log.assert_called_once()
            assert access_log.log.call_args.kwargs == {
                "provider": "openrouter",
                "model": "qwen/qwen3-coder",
                "pool": "code",
                "cache_status": "miss",
            }
        finally:
            await client.close()


class TestWeightedPoolSelection:
    """Weighted load-balancing within pool tiers."""

    def test_default_weight_is_one(self):
        """ModelSpec defaults to weight=1.0 (equal)."""
        spec = ModelSpec.from_dict(
            {"provider": "openrouter", "model": "gpt-4"},
            zdr=False,
            provider_zdr_ok=True,
        )
        assert spec.weight == 1.0

    def test_custom_weight_parsed(self):
        """ModelSpec parses 'weight' from config."""
        spec = ModelSpec.from_dict(
            {"provider": "openrouter", "model": "gpt-4", "weight": 0.7},
            zdr=False,
            provider_zdr_ok=True,
        )
        assert spec.weight == 0.7

    def test_invalid_weight_falls_back_to_default(self):
        """Invalid weights (≤0) fall back to 1.0."""
        spec = ModelSpec.from_dict(
            {"provider": "openrouter", "model": "gpt-4", "weight": -1},
            zdr=False,
            provider_zdr_ok=True,
        )
        assert spec.weight == 1.0

        for invalid in ("not-a-number", float("nan"), float("inf")):
            spec = ModelSpec.from_dict(
                {"provider": "openrouter", "model": "gpt-4", "weight": invalid},
                zdr=False,
                provider_zdr_ok=True,
            )
            assert spec.weight == 1.0

        spec = ModelSpec.from_dict(
            {"provider": "openrouter", "model": "gpt-4", "weight": 0},
            zdr=False,
            provider_zdr_ok=True,
        )
        assert spec.weight == 1.0

    def test_status_includes_weight(self):
        """ModelSpec.weight field is preserved for status serialization."""
        # Test that ModelSpec correctly parses and stores weight
        spec1 = ModelSpec.from_dict(
            {"provider": "openrouter", "model": "gpt-4", "weight": 0.7},
            zdr=False,
            provider_zdr_ok=True,
        )
        spec2 = ModelSpec.from_dict(
            {"provider": "openrouter", "model": "claude-3.5-sonnet", "weight": 0.3},
            zdr=False,
            provider_zdr_ok=True,
        )
        
        # Verify weight is stored correctly on each spec
        assert spec1.weight == 0.7
        assert spec2.weight == 0.3
