"""Tests for standards-based audio routing (speech input and output)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tusker_gateway.config import load_config
from tusker_gateway.context_limits import estimate_prompt_tokens
from tusker_gateway.errors import GatewayError
from tusker_gateway.providers.stt import STTHandler


class _FakeResp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return self._body.decode("utf-8", "replace")

    async def json(self, **_kwargs):
        return json.loads(self._body)


class _RecordingSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, url, *, headers=None, data=None, **kwargs):
        self.calls.append({"url": url, "headers": headers, "data": data})
        return self.response


def test_audio_routes_registered_on_the_real_app():
    """Both OpenAI-compatible audio endpoints must be reachable."""
    from tusker_gateway.app import create_app

    app = create_app()
    paths = {route.resource.canonical for route in app.router.routes()}
    assert "/v1/audio/speech" in paths
    assert "/v1/audio/transcriptions" in paths


def test_stt_provider_selection():
    h = STTHandler({})
    assert h.get_provider_for_stt_request("whisper-1") == "openai"
    assert h.get_provider_for_stt_request("groq::whisper-large-v3-turbo") == "groq"
    assert h.get_provider_for_stt_request("groq/whisper-large-v3") == "groq"
    assert h.get_provider_for_stt_request("openai/whisper-1") == "openai"
    assert h.get_provider_for_stt_request("openrouter/some/stt") == "openrouter"


def test_stt_model_normalization():
    assert STTHandler.normalize_model("groq::whisper-large-v3", "groq") == "whisper-large-v3"
    assert STTHandler.normalize_model("groq/whisper-large-v3", "groq") == "whisper-large-v3"
    assert STTHandler.normalize_model("whisper-1", "openai") == "whisper-1"


@pytest.mark.asyncio
async def test_stt_posts_multipart_and_returns_transcript():
    h = STTHandler({})
    session = _RecordingSession(
        _FakeResp(body=b'{"text":"hello world","language":"en"}')
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h.handle_request(
            model="groq::whisper-large-v3-turbo",
            form_fields={"language": "en", "temperature": "0"},
            audio_bytes=b"RIFFfake-wav-bytes",
            filename="voice.wav",
            content_type="audio/wav",
            api_key="gsk-test",
        )
    assert result["text"] == "hello world"
    call = session.calls[0]
    assert call["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert call["headers"]["Authorization"] == "Bearer gsk-test"
    # FormData is built with the standard field names; verify by rendering it.
    body = call["data"]
    rendered = body()
    text = rendered.decode("utf-8", "replace")
    assert 'name="file"' in text
    assert 'filename="voice.wav"' in text
    assert 'name="model"' in text
    assert "whisper-large-v3-turbo" in text
    assert 'name="language"' in text


@pytest.mark.asyncio
async def test_stt_rejects_empty_and_oversized_audio():
    h = STTHandler({})
    with pytest.raises(GatewayError) as ei:
        await h.handle_request(
            model="whisper-1", form_fields={}, audio_bytes=b"",
            filename="x.wav", content_type="audio/wav", api_key="k",
        )
    assert ei.value.code == "invalid_request"
    with pytest.raises(GatewayError) as ei:
        await h.handle_request(
            model="whisper-1", form_fields={},
            audio_bytes=b"x" * (STTHandler.MAX_STT_AUDIO_BYTES + 1),
            filename="x.wav", content_type="audio/wav", api_key="k",
        )
    assert ei.value.code == "payload_too_large"


@pytest.mark.asyncio
async def test_stt_requires_api_key():
    h = STTHandler({})
    with pytest.raises(GatewayError) as ei:
        await h.handle_request(
            model="whisper-1", form_fields={}, audio_bytes=b"RIFF",
            filename="x.wav", content_type="audio/wav", api_key=None,
        )
    assert ei.value.code == "missing_api_key"


def test_audio_content_parts_consume_context_budget(monkeypatch):
    monkeypatch.setenv("TUSKER_AUDIO_TOKEN_ESTIMATE", "500")
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        ],
    }
    with_audio = estimate_prompt_tokens([message])
    without_audio = estimate_prompt_tokens(
        [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    )
    assert with_audio - without_audio == 500


@pytest.mark.asyncio
async def test_transcription_endpoint_accepts_multipart_and_returns_text():
    """The endpoint parses a standard OpenAI-shaped multipart upload."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from tusker_gateway.endpoints import transcription_handler

    captured: dict[str, object] = {}

    class _FakeSTT:
        MAX_STT_AUDIO_BYTES = 25 * 1024 * 1024

        def get_provider_for_stt_request(self, model):
            return "groq"

        async def handle_request(self, **kwargs):
            captured.update(kwargs)
            return {"text": "voice control acknowledged"}

    app = web.Application()
    app["config"] = {"provider_api_keys": {"groq": "gsk-test"}}
    app["stt_handler"] = _FakeSTT()
    app.router.add_post("/v1/audio/transcriptions", transcription_handler)
    import aiohttp

    form = aiohttp.FormData()
    form.add_field(
        "file", b"RIFFfake-wav-bytes",
        filename="voice.wav", content_type="audio/wav",
    )
    form.add_field("model", "groq::whisper-large-v3-turbo")
    form.add_field("language", "en")
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/v1/audio/transcriptions", data=form)
        assert resp.status == 200
        assert (await resp.json())["text"] == "voice control acknowledged"
        assert captured["audio_bytes"] == b"RIFFfake-wav-bytes"
        assert captured["filename"] == "voice.wav"
        assert captured["model"] == "groq::whisper-large-v3-turbo"
        assert captured["form_fields"]["language"] == "en"
    finally:
        await client.close()


def test_transcription_capability_models_registered():
    from tusker_gateway.providers.capabilities import Capability, _OPENAI_STATIC_MODELS

    entries = _OPENAI_STATIC_MODELS[Capability.SPEECH_TRANSCRIPTION]
    assert {e.provider for e in entries} == {"openai"}
    assert any(e.model == "whisper-1" for e in entries)
