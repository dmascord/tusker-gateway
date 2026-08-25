"""Tests for TTS and video generation handlers."""
import base64
import json
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tusker_gateway.errors import GatewayError
from tusker_gateway.providers.tts import TTSHandler
from tusker_gateway.providers.video import VideoHandler


# ---------- TTS ----------


class _FakeContent:
    """Fake aiohttp response.content that yields a single buffer."""

    def __init__(self, body: bytes = b""):
        self._body = body
        self._done = False

    async def iter_any(self):
        if not self._done:
            self._done = True
            yield self._body

    async def iter_chunked(self, _size):
        if not self._done:
            self._done = True
            yield self._body


class _FakeResp:
    def __init__(self, status: int = 200, body: bytes = b"", headers: Optional[dict] = None):
        self.status = status
        self._content = _FakeContent(body)
        self._content_bytes = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def content(self):
        return self._content

    async def text(self):
        return self._content_bytes.decode("utf-8", errors="replace")

    async def json(self):
        return json.loads(self._content_bytes or b"{}")

    async def read(self):
        return self._content_bytes


class _FakeSession:
    """Implements both the ctx-manager and `.post`/`get` surface."""

    def __init__(self, *a, **kw):
        self._responses: list[_FakeResp] = []

    def add(self, resp: _FakeResp):
        self._responses.append(resp)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, headers=None, json=None, **kw):
        return self._responses.pop(0)

    def get(self, url, headers=None, **kw):
        return self._responses.pop(0)


def _mp3_bytes() -> bytes:
    # Magic bytes for an MP3 with ID3v2 header.
    return b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 64


@pytest.mark.asyncio
async def test_tts_dispatches_openai_for_bare_model():
    h = TTSHandler({})
    assert h.get_provider_for_tts_request("tts-1") == "openai"
    assert h.get_provider_for_tts_request("tts-1-hd") == "openai"
    assert h.get_provider_for_tts_request("gpt-4o-mini-tts") == "openai"


@pytest.mark.asyncio
async def test_tts_dispatches_openrouter_for_slash_model():
    h = TTSHandler({})
    assert h.get_provider_for_tts_request("openai/gpt-4o-mini-tts") == "openrouter"
    assert h.get_provider_for_tts_request("mistralai/voxtral-mini-tts") == "openrouter"


@pytest.mark.asyncio
async def test_tts_dispatches_xiaomi_models_and_pin():
    h = TTSHandler({})
    assert h.get_provider_for_tts_request("mimo-v2.5-tts") == "xiaomi"
    assert h.get_provider_for_tts_request("mimo-v2.5-tts-voicedesign") == "xiaomi"
    assert h.get_provider_for_tts_request("xiaomi::mimo-v2.5-tts") == "xiaomi"


@pytest.mark.asyncio
async def test_tts_openai_returns_audio_bytes():
    h = TTSHandler({})
    audio = _mp3_bytes()
    session = _FakeSession().add(
        _FakeResp(status=200, body=audio, headers={"Content-Type": "audio/mpeg"})
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        out_bytes, content_type = await h._call_openai(
            model="tts-1",
            body={"input": "hello", "voice": "alloy", "response_format": "mp3"},
            api_key="sk-test",
            extra_headers=None,
        )
    assert out_bytes == audio
    assert content_type == "audio/mpeg"


@pytest.mark.asyncio
async def test_tts_openai_raises_on_4xx():
    h = TTSHandler({})
    session = _FakeSession().add(_FakeResp(status=401, body=b'{"error":"bad key"}'))
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        with pytest.raises(GatewayError) as ei:
            await h._call_openai(
                model="tts-1",
                body={"input": "hi"},
                api_key="sk-test",
                extra_headers=None,
            )
    assert "401" in str(ei.value)


@pytest.mark.asyncio
async def test_tts_requires_api_key():
    h = TTSHandler({})
    with pytest.raises(GatewayError) as ei:
        await h._call_openai(
            model="tts-1", body={"input": "hi"}, api_key=None, extra_headers=None
        )
    assert "API key" in str(ei.value)


@pytest.mark.asyncio
async def test_tts_validates_response_format():
    h = TTSHandler({})
    with pytest.raises(GatewayError) as ei:
        await h.handle_request(
            model="tts-1",
            body={"input": "hi", "response_format": "wav-extra"},
            api_key="sk-test",
        )
    assert "unsupported response_format" in str(ei.value)


@pytest.mark.asyncio
async def test_tts_validates_input():
    h = TTSHandler({})
    with pytest.raises(GatewayError) as ei:
        await h.handle_request(model="tts-1", body={}, api_key="sk-test")
    assert "input" in str(ei.value)


@pytest.mark.asyncio
async def test_tts_openrouter_path():
    h = TTSHandler({})
    audio = _mp3_bytes()
    session = _FakeSession().add(
        _FakeResp(status=200, body=audio, headers={"Content-Type": "audio/mpeg"})
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        out_bytes, content_type = await h._call_openrouter(
            model="openai/gpt-4o-mini-tts",
            body={"input": "hi", "voice": "alloy"},
            api_key="sk-test",
            extra_headers=None,
        )
    assert out_bytes == audio



@pytest.mark.asyncio
async def test_tts_xiaomi_translates_chat_request_and_decodes_audio():
    h = TTSHandler({})
    audio = b"RIFF" + b"\x00" * 64
    upstream = {
        "choices": [{"message": {"audio": {"data": base64.b64encode(audio).decode()}}}]
    }
    session = _FakeSession().add(
        _FakeResp(status=200, body=json.dumps(upstream).encode())
    )
    calls = []
    real_post = session.post

    def post(url, headers=None, json=None, **kwargs):
        calls.append((url, headers, json))
        return real_post(url, headers=headers, json=json, **kwargs)

    session.post = post
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        out_bytes, content_type = await h._call_xiaomi(
            model="xiaomi::mimo-v2.5-tts",
            body={
                "input": "Hello from Tusker.",
                "voice": "Mia",
                "response_format": "wav",
                "instructions": "Speak clearly.",
            },
            api_key="tp-test",
            extra_headers=None,
        )

    assert out_bytes == audio
    assert content_type == "audio/wav"
    assert calls == [(
        "https://token-plan-sgp.xiaomimimo.com/v1/chat/completions",
        {"Authorization": "Bearer tp-test", "Content-Type": "application/json"},
        {
            "model": "mimo-v2.5-tts",
            "messages": [
                {"role": "user", "content": "Speak clearly."},
                {"role": "assistant", "content": "Hello from Tusker."},
            ],
            "audio": {"format": "wav", "voice": "Mia"},
            "stream": False,
        },
    )]


def test_tts_xiaomi_voice_design_uses_instructions_without_voice():
    payload, fmt = TTSHandler._normalise_xiaomi_request(
        "mimo-v2.5-tts-voicedesign",
        {
            "input": "Designed speech.",
            "instructions": "A warm, confident young voice.",
            "voice": "ignored",
            "response_format": "mp3",
        },
    )

    assert fmt == "mp3"
    assert payload["messages"] == [
        {"role": "user", "content": "A warm, confident young voice."},
        {"role": "assistant", "content": "Designed speech."},
    ]
    assert payload["audio"] == {"format": "mp3"}


def test_tts_xiaomi_voice_clone_rejects_non_audio_voice():
    with pytest.raises(GatewayError) as exc_info:
        TTSHandler._normalise_xiaomi_request(
            "mimo-v2.5-tts-voiceclone",
            {"input": "Cloned speech.", "voice": "Mia"},
        )

    assert "data URI" in str(exc_info.value)


@pytest.mark.asyncio
async def test_tts_xiaomi_rejects_missing_audio_response():
    h = TTSHandler({})
    session = _FakeSession().add(
        _FakeResp(status=200, body=b'{"choices":[{"message":{}}]}')
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        with pytest.raises(GatewayError) as exc_info:
            await h._call_xiaomi(
                model="mimo-v2.5-tts",
                body={"input": "Hello"},
                api_key="tp-test",
                extra_headers=None,
            )

    assert "invalid audio response" in str(exc_info.value)

# ---------- Video ----------


def _make_video_job(status: str = "queued", job_id: str = "video_abc") -> dict:
    return {
        "id": job_id,
        "object": "video",
        "status": status,
        "progress": 0,
        "model": "sora-2",
        "seconds": "5",
        "size": "1280x720",
    }


@pytest.mark.asyncio
async def test_video_provider_selection():
    h = VideoHandler({})
    assert h.get_provider_for_video_request("sora-2") == "openai"
    assert h.get_provider_for_video_request("openai/sora-2-pro") == "openrouter"
    assert h.get_provider_for_video_request("MiniMax-H3") == "minimax"


@pytest.mark.asyncio
async def test_video_request_validation():
    h = VideoHandler({})
    with pytest.raises(GatewayError) as ei:
        await h._normalise_request("sora-2", {})
    assert "prompt" in str(ei.value)


@pytest.mark.asyncio
async def test_video_openai_polls_until_complete():
    h = VideoHandler({})
    session = (
        _FakeSession()
        .add(_FakeResp(status=200, body=json.dumps(_make_video_job("queued")).encode()))
        .add(_FakeResp(status=200, body=json.dumps(_make_video_job("in_progress")).encode()))
        .add(_FakeResp(status=200, body=json.dumps(_make_video_job("completed")).encode()))
        .add(_FakeResp(status=200, body=b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32))
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h._call_openai(
            model="sora-2",
            body={"prompt": "A cat", "size": "1280x720", "seconds": "5"},
            api_key="sk-test",
            extra_headers=None,
            wait=True,
            poll_interval=0.001,
            max_wait=10.0,
        )
    assert result["status"] == "completed"
    assert "b64_json" in result


@pytest.mark.asyncio
async def test_video_openai_no_wait_returns_initial():
    h = VideoHandler({})
    job = _make_video_job("queued")
    session = _FakeSession().add(
        _FakeResp(status=200, body=json.dumps(job).encode())
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h._call_openai(
            model="sora-2",
            body={"prompt": "A cat"},
            api_key="sk-test",
            extra_headers=None,
            wait=False,
        )
    assert result["status"] == "queued"
    assert result["id"] == job["id"]


@pytest.mark.asyncio
async def test_video_openai_failed_status_raises():
    h = VideoHandler({})
    session = (
        _FakeSession()
        .add(_FakeResp(status=200, body=json.dumps(_make_video_job("queued")).encode()))
        .add(_FakeResp(status=200, body=json.dumps({**_make_video_job("failed"), "error": "bad prompt"}).encode()))
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        with pytest.raises(GatewayError) as ei:
            await h._call_openai(
                model="sora-2",
                body={"prompt": "A cat"},
                api_key="sk-test",
                extra_headers=None,
                wait=True,
                poll_interval=0.001,
                max_wait=10.0,
            )
    assert "failed" in str(ei.value)


@pytest.mark.asyncio
async def test_video_openai_requires_api_key():
    h = VideoHandler({})
    with pytest.raises(GatewayError) as ei:
        await h._call_openai(
            model="sora-2",
            body={"prompt": "A cat"},
            api_key=None,
            extra_headers=None,
            wait=False,
        )
    assert "API key" in str(ei.value)


@pytest.mark.asyncio
async def test_video_openai_create_4xx_raises():
    h = VideoHandler({})
    session = _FakeSession().add(
        _FakeResp(status=400, body=b'{"error":"bad request"}')
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        with pytest.raises(GatewayError) as ei:
            await h._call_openai(
                model="sora-2",
                body={"prompt": "A cat"},
                api_key="sk-test",
                extra_headers=None,
                wait=False,
            )
    assert "400" in str(ei.value)


@pytest.mark.asyncio
async def test_video_openrouter_polls_until_complete():
    h = VideoHandler({})
    create = {
        **_make_video_job("pending"),
        "polling_url": "https://openrouter.ai/api/v1/videos/video_abc",
    }
    completed = {**_make_video_job("completed")}
    session = (
        _FakeSession()
        .add(_FakeResp(status=202, body=json.dumps(create).encode()))
        .add(_FakeResp(status=200, body=json.dumps(completed).encode()))
        .add(_FakeResp(status=200, body=b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32))
    )
    calls = []
    real_post = session.post
    real_get = session.get

    def post(url, headers=None, json=None, **kwargs):
        calls.append(("POST", url, headers, json))
        return real_post(url, headers=headers, json=json, **kwargs)

    def get(url, headers=None, **kwargs):
        calls.append(("GET", url, headers, None))
        return real_get(url, headers=headers, **kwargs)

    session.post = post
    session.get = get
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h._call_openrouter(
            model="openai/sora-2",
            body={"prompt": "A cat"},
            api_key="sk-test",
            extra_headers=None,
            wait=True,
            poll_interval=0.001,
            max_wait=10.0,
        )
    assert result["status"] == "completed"
    assert "b64_json" in result
    assert calls[0][1] == "https://openrouter.ai/api/v1/videos"
    assert calls[0][3]["model"] == "openai/sora-2"
    assert calls[1][1] == create["polling_url"]
    assert calls[2][1] == "https://openrouter.ai/api/v1/videos/video_abc/content?index=0"
    assert calls[2][2]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_video_openai_already_completed_on_first_poll():
    h = VideoHandler({})
    session = (
        _FakeSession()
        .add(_FakeResp(status=200, body=json.dumps(_make_video_job("completed")).encode()))
        .add(_FakeResp(status=200, body=b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32))
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h._call_openai(
            model="sora-2",
            body={"prompt": "A cat"},
            api_key="sk-test",
            extra_headers=None,
            wait=True,
            poll_interval=0.001,
            max_wait=10.0,
        )
    assert result["status"] == "completed"
    assert "b64_json" in result


@pytest.mark.asyncio
async def test_video_zai_dispatches_cogvideox_and_vidu():
    """cogvideox-* / vidu* model slugs route to zai provider."""
    h = VideoHandler({})
    assert h.get_provider_for_video_request("cogvideox-3") == "zai"
    assert h.get_provider_for_video_request("viduq1-text") == "zai"
    # Sanity: openrouter slash-model still wins.
    assert h.get_provider_for_video_request("cogvideox-3/openrouter") == "openrouter"


@pytest.mark.asyncio
async def test_video_openrouter_rejects_external_polling_url():
    h = VideoHandler({})
    session = _FakeSession().add(
        _FakeResp(status=200, body=json.dumps({"status": "completed"}).encode())
    )
    calls = []
    real_get = session.get

    def get(url, headers=None, **kwargs):
        calls.append((url, headers, kwargs))
        return real_get(url, headers=headers, **kwargs)

    session.get = get
    job = {
        "id": "video_abc",
        "polling_url": "http://openrouter.ai:8443/steal",
    }
    result = await h._poll_openrouter(
        session, job, "sk-test", poll_interval=0.001, max_wait=1.0
    )

    assert result["status"] == "completed"
    assert calls[0][0] == "https://openrouter.ai/api/v1/videos/video_abc"
    assert calls[0][1]["Authorization"] == "Bearer sk-test"
    assert calls[0][2]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_video_zai_create_polls_until_success_and_returns_signed_url():
    """Create → poll → return the provider result URL without server-side fetch."""
    h = VideoHandler({})
    session = _FakeSession()
    session.add(_FakeResp(200, json.dumps({"id": "vid-1"}).encode()))
    session.add(_FakeResp(200, json.dumps({"task_status": "PROCESSING"}).encode()))
    session.add(_FakeResp(200, json.dumps({"task_status": "PROCESSING"}).encode()))
    session.add(
        _FakeResp(
            200,
            json.dumps(
                {
                    "task_status": "SUCCESS",
                    "video_result": [{"url": "https://signed.test/out.mp4"}],
                }
            ).encode(),
        )
    )

    captured: dict = {}
    real_post = session.post
    real_get = session.get

    def post(url, headers=None, json=None, **kw):
        captured["create_url"] = url
        captured["create_headers"] = headers
        captured["create_body"] = json
        return real_post(url, headers=headers, json=json, **kw)

    def get(url, headers=None, **kw):
        captured.setdefault("gets", []).append((url, headers))
        return real_get(url, headers=headers, **kw)

    session.post = post
    session.get = get

    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h._call_zai(
            model="cogvideox-3",
            body={"prompt": "A wave", "size": "1280x720"},
            api_key="zai-key",
            extra_headers=None,
            wait=True,
            poll_interval=0.001,
            max_wait=5.0,
        )

    assert captured["create_url"] == "https://api.z.ai/api/paas/v4/videos/generations"
    assert captured["create_headers"]["Authorization"] == "Bearer zai-key"
    assert captured["create_body"]["model"] == "cogvideox-3"
    assert captured["create_body"]["prompt"] == "A wave"
    assert captured["create_body"]["size"] == "1280x720"
    assert [u for (u, _) in captured["gets"]] == [
        "https://api.z.ai/api/paas/v4/async-result/vid-1",
        "https://api.z.ai/api/paas/v4/async-result/vid-1",
        "https://api.z.ai/api/paas/v4/async-result/vid-1",
    ]
    assert result["video_result"][0]["url"] == "https://signed.test/out.mp4"
    assert "b64_json" not in result


@pytest.mark.asyncio
async def test_video_zai_requires_api_key():
    h = VideoHandler({})
    with pytest.raises(GatewayError) as ei:
        await h._call_zai(
            model="cogvideox-3",
            body={"prompt": "A wave"},
            api_key=None,
            extra_headers=None,
            wait=True,
            poll_interval=0.001,
            max_wait=5.0,
        )
    assert ei.value.code == "missing_api_key"




@pytest.mark.asyncio
async def test_video_zai_no_wait_returns_initial_job():
    """wait=false → return the create response immediately, no polling."""
    h = VideoHandler({})
    session = _FakeSession()
    initial = {"id": "vid-2", "task_status": "PROCESSING"}
    session.add(_FakeResp(200, json.dumps(initial).encode()))

    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h._call_zai(
            model="viduq1-text",
            body={"prompt": "A flower blooms"},
            api_key="zai-key",
            extra_headers=None,
            wait=False,
            poll_interval=0.001,
            max_wait=5.0,
        )

    assert result["id"] == "vid-2"
    assert result["task_status"] == "PROCESSING"


@pytest.mark.asyncio
async def test_video_zai_poll_fail_status_raises():
    """FAIL status from the async-result endpoint surfaces as upstream_error."""
    h = VideoHandler({})
    session = _FakeSession()
    session.add(_FakeResp(200, json.dumps({"id": "vid-3"}).encode()))
    session.add(
        _FakeResp(
            200,
            json.dumps({"task_status": "FAIL", "error": "bad prompt"}).encode(),
        )
    )

    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        with pytest.raises(GatewayError) as ei:
            await h._call_zai(
                model="cogvideox-3",
                body={"prompt": "A wave"},
                api_key="zai-key",
                extra_headers=None,
                wait=True,
                poll_interval=0.001,
                max_wait=5.0,
            )

    assert ei.value.code == "upstream_error"
    assert "FAIL" in str(ei.value) or "bad prompt" in str(ei.value)



@pytest.mark.asyncio
async def test_video_minimax_pin_dispatches_and_no_wait_normalises_create():
    h = VideoHandler({})
    assert h.get_provider_for_video_request("minimax::MiniMax-H3") == "minimax"

    session = _FakeSession().add(
        _FakeResp(200, json.dumps({"task_id": "task-h3-1"}).encode())
    )
    captured: dict = {}
    real_post = session.post

    def post(url, headers=None, json=None, **kwargs):
        captured.update(url=url, headers=headers, body=json, kwargs=kwargs)
        return real_post(url, headers=headers, json=json, **kwargs)

    session.post = post
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h.handle_request(
            model="minimax::MiniMax-H3",
            body={
                "prompt": "A wave curls over a lighthouse",
                "seconds": "8",
                "resolution": "768P",
                "ratio": "16:9",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://assets.test/start.png"},
                        "role": "reference_image",
                    }
                ],
            },
            api_key="minimax-key",
            wait=False,
        )

    assert captured["url"] == "https://api.minimax.io/v2/video_generation"
    assert captured["headers"]["Authorization"] == "Bearer minimax-key"
    assert captured["kwargs"]["allow_redirects"] is False
    assert captured["body"] == {
        "model": "MiniMax-H3",
        "content": [
            {"type": "text", "text": "A wave curls over a lighthouse"},
            {
                "type": "image_url",
                "image_url": {"url": "https://assets.test/start.png"},
                "role": "reference_image",
            },
        ],
        "duration": 8,
        "resolution": "768P",
        "ratio": "16:9",
    }
    assert result == {
        "id": "task-h3-1",
        "task_id": "task-h3-1",
        "model": "MiniMax-H3",
        "provider": "minimax",
        "status": "queued",
        "task": {"task_id": "task-h3-1"},
    }


@pytest.mark.asyncio
async def test_video_minimax_polls_and_returns_completed_url_without_download():
    h = VideoHandler({})
    session = (
        _FakeSession()
        .add(_FakeResp(200, json.dumps({"task_id": "task-h3-2"}).encode()))
        .add(
            _FakeResp(
                200,
                json.dumps({"task": {"id": "task-h3-2", "status": "running"}}).encode(),
            )
        )
        .add(
            _FakeResp(
                200,
                json.dumps(
                    {
                        "task": {
                            "id": "task-h3-2",
                            "status": "succeeded",
                            "content": {"url": "https://cdn.minimax.io/h3/output.mp4"},
                        }
                    }
                ).encode(),
            )
        )
    )
    calls = []
    real_get = session.get

    def get(url, headers=None, **kwargs):
        calls.append((url, headers, kwargs))
        return real_get(url, headers=headers, **kwargs)

    session.get = get
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h._call_minimax(
            model="minimax::MiniMax-H3",
            body={"prompt": "Clouds racing", "duration": 6, "resolution": "2K"},
            api_key="minimax-key",
            extra_headers=None,
            wait=True,
            poll_interval=0.001,
            max_wait=5.0,
        )

    assert [call[0] for call in calls] == [
        "https://api.minimax.io/v2/query/video_generation/task-h3-2",
        "https://api.minimax.io/v2/query/video_generation/task-h3-2",
    ]
    assert all(call[1]["Authorization"] == "Bearer minimax-key" for call in calls)
    assert result["status"] == "completed"
    assert result["url"] == "https://cdn.minimax.io/h3/output.mp4"
    assert result["video"] == {"url": "https://cdn.minimax.io/h3/output.mp4"}
    assert "b64_json" not in result
    assert session._responses == []


@pytest.mark.asyncio
async def test_video_minimax_requires_api_key():
    h = VideoHandler({})
    with pytest.raises(GatewayError) as ei:
        await h._call_minimax(
            model="minimax::MiniMax-H3",
            body={"prompt": "Clouds racing"},
            api_key=None,
            extra_headers=None,
            wait=False,
        )
    assert ei.value.code == "missing_api_key"
    assert "MiniMax" in str(ei.value)


@pytest.mark.asyncio
async def test_video_minimax_create_upstream_failure_is_not_masked():
    h = VideoHandler({})
    session = _FakeSession().add(
        _FakeResp(
            400,
            b'{"base_resp":{"status_code":1008,"status_msg":"PAYG required"}}',
        )
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        with pytest.raises(GatewayError) as ei:
            await h._call_minimax(
                model="minimax::MiniMax-H3",
                body={"prompt": "Clouds racing"},
                api_key="token-plan-key",
                extra_headers=None,
                wait=False,
            )
    assert ei.value.code == "upstream_error"
    assert "400" in str(ei.value)
    assert "PAYG required" in str(ei.value)


@pytest.mark.asyncio
async def test_video_minimax_poll_upstream_failure_is_not_masked():
    h = VideoHandler({})
    session = (
        _FakeSession()
        .add(_FakeResp(200, json.dumps({"task_id": "task-h3-3"}).encode()))
        .add(_FakeResp(401, b'{"error":"unauthorized"}'))
    )
    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        with pytest.raises(GatewayError) as ei:
            await h._call_minimax(
                model="minimax::MiniMax-H3",
                body={"prompt": "Clouds racing"},
                api_key="bad-key",
                extra_headers=None,
                wait=True,
                poll_interval=0.001,
                max_wait=5.0,
            )
    assert ei.value.code == "upstream_error"
    assert "poll 401" in str(ei.value)
