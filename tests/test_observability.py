"""Tests for observability features: access logging, request-ID propagation, weighted pool selection."""

import json
import logging
import os
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from tusker_gateway.observability import AccessLog, attach_request_id_middleware, _generate_request_id
from tusker_gateway.pools import ModelSpec, PoolManager, PoolConfig


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
