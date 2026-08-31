"""Tests for persisted model capability evidence and input probes."""
from __future__ import annotations

import json
import time
from typing import Any

import pytest

from tusker_gateway.model_capability import (
    MODEL_CAPABILITY_PROBE_VERSION,
    ModelCapabilityDB,
)
from tusker_gateway.modality_qualification import (
    _classify_http_failure,
    _messages_for_modality,
    probe_input_model,
)
from tusker_gateway.endpoints import _record_media_capabilities


class _Response:
    def __init__(self, status: int, body: Any):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self) -> bytes:
        if isinstance(self._body, bytes):
            return self._body
        return json.dumps(self._body).encode()

    async def json(self) -> Any:
        return self._body


class _Session:
    def __init__(self, response: _Response):
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        return self.response


def test_model_capability_db_round_trip_and_catalog_does_not_downgrade_probe(tmp_path):
    db = ModelCapabilityDB(str(tmp_path / "model-capability.db"))

    passed = db.record(
        provider="OpenRouter",
        model="vision/model",
        capability="input-image",
        status="passed",
        source="modality_probe",
        http_status=200,
        latency_ms=42.5,
    )
    assert passed.provider == "openrouter"
    assert passed.capability == "input_image"
    assert passed.verified is True

    retained = db.record(
        provider="openrouter",
        model="vision/model",
        capability="input_image",
        status="advertised",
        source="catalog",
    )
    assert retained.status == "passed"
    assert db.get("openrouter", "vision/model", "input_image").status == "passed"

    summary = db.status()
    assert summary["total_records"] == 1
    assert summary["verified_models"] == 1
    assert summary["by_capability"]["input_image"]["passed"] == 1


def test_model_capability_db_keeps_unsupported_as_authoritative(tmp_path):
    db = ModelCapabilityDB(str(tmp_path / "model-capability.db"))
    db.record(
        provider="groq",
        model="text-only",
        capability="input_image",
        status="unsupported",
        source="modality_probe",
        http_status=400,
        failure_class="modality_rejected",
    )
    db.record(
        provider="groq",
        model="text-only",
        capability="input_image",
        status="discovered",
        source="capability_registry",
    )
    record = db.get("groq", "text-only", "input_image")
    assert record is not None
    assert record.status == "unsupported"
    assert record.transient is False


@pytest.mark.asyncio
async def test_probe_input_model_records_only_safe_result_shape():
    session = _Session(_Response(200, {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    }))

    result = await probe_input_model(
        session,
        base_url="http://gateway.test",
        api_key="gateway-key",
        provider="openrouter",
        model="vision/model",
        modality="image",
    )

    assert result["status"] == "passed"
    assert result["capability"] == "input_image"
    assert result["http_status"] == 200
    assert "body" not in result
    assert session.calls[0][0] == "http://gateway.test/v1/chat/completions"
    payload = session.calls[0][1]["json"]
    assert payload["model"] == "openrouter::vision/model"
    assert payload["messages"][0]["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_probe_input_model_separates_unsupported_from_transient():
    unsupported = _Session(_Response(400, {"error": "image input not supported"}))
    result = await probe_input_model(
        unsupported,
        base_url="http://gateway.test",
        api_key="gateway-key",
        provider="groq",
        model="text-only",
        modality="image",
    )
    assert result["status"] == "unsupported"
    assert result["failure_class"] == "modality_rejected"

    assert _classify_http_failure(429, "global quota exceeded") == (
        "unavailable",
        "rate_limited",
    )
    assert _classify_http_failure(401, "unauthorized") == ("unavailable", "auth")


def test_probe_payloads_cover_supported_input_shapes():
    assert _messages_for_modality("text")[0]["content"]
    assert _messages_for_modality("image")[0]["content"][1]["type"] == "image_url"
    assert _messages_for_modality("audio")[0]["content"][1]["type"] == "input_audio"
    assert _messages_for_modality("video")[0]["content"][1]["type"] == "video_url"
    assert MODEL_CAPABILITY_PROBE_VERSION == "model-capability-v1"


def test_successful_media_capability_records_are_provider_model_scoped(tmp_path):
    db = ModelCapabilityDB(str(tmp_path / "model-capability.db"))

    class Request:
        app = {"model_capabilities": db}

    _record_media_capabilities(
        Request(),
        provider="openrouter",
        model="openrouter::google/gemini-image",
        capabilities=("output_image", "image_generations"),
        started=time.monotonic(),
    )

    assert db.get("openrouter", "google/gemini-image", "output_image").status == "passed"
    assert db.get("openrouter", "google/gemini-image", "image_generations").status == "passed"
