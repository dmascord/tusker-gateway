"""Offline tests for embedding/rerank capability qualification."""
from __future__ import annotations

import json

import pytest

from tusker_gateway.media_health import media_capability_allowed
from tusker_gateway.media_qualification import probe_media_route


class _Response:
    def __init__(self, status: int, body: object):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.body


class _Session:
    def __init__(self, response: _Response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_embedding_probe_records_safe_success_metadata():
    session = _Session(_Response(200, {"data": [{"embedding": [0.1]}]}))
    result = await probe_media_route(
        session,
        base_url="http://gateway.test",
        api_key="gateway-key",
        provider="openrouter",
        model="embed-model",
        capability="embedding",
    )
    assert result["status"] == "passed"
    assert result["http_status"] == 200
    assert "body" not in result
    assert session.calls[0][0] == "http://gateway.test/v1/embeddings"
    assert session.calls[0][1]["json"] == {"model": "openrouter::embed-model", "input": "qualification"}


@pytest.mark.asyncio
async def test_rerank_probe_preserves_transient_failure_classification():
    session = _Session(_Response(429, {"error": {"message": "quota"}}))
    result = await probe_media_route(
        session,
        base_url="http://gateway.test",
        api_key="gateway-key",
        provider="cohere",
        model="rerank-v3.5",
        capability="rerank",
    )
    assert result["status"] == "unavailable"
    assert result["failure_class"] == "provider_unavailable"
    assert "quota" not in json.dumps(result)


@pytest.mark.asyncio
async def test_media_probe_classifies_unsupported_gateway_route():
    session = _Session(_Response(400, {"error": {"code": "no_reranker_providers"}}))
    result = await probe_media_route(
        session,
        base_url="http://gateway.test",
        api_key="gateway-key",
        provider="cohere",
        model="rerank-v3.5",
        capability="rerank",
    )
    assert result["status"] == "unsupported"
    assert result["failure_class"] == "no_reranker_providers"

def test_media_capability_gate_rejects_fresh_unsupported_record(tmp_path, monkeypatch):
    from tusker_gateway.media_qualification import (
        EMBEDDING_CAPABILITY,
        MEDIA_CAPABILITY_PROBE_VERSION,
    )
    from tusker_gateway.model_capability import ModelCapabilityDB

    monkeypatch.setenv("TUSKER_MEDIA_CAPABILITY_GATE_ENABLED", "true")
    db = ModelCapabilityDB(str(tmp_path / "capabilities.db"))
    db.record(
        provider="synthetic",
        model="hf:nomic-ai/nomic-embed-text-v1.5",
        capability=EMBEDDING_CAPABILITY,
        status="unsupported",
        source="media_probe",
        probe_version=MEDIA_CAPABILITY_PROBE_VERSION,
    )

    assert not media_capability_allowed(
        db,
        provider="synthetic",
        model="hf:nomic-ai/nomic-embed-text-v1.5",
        capability=EMBEDDING_CAPABILITY,
    )
