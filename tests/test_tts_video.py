"""Tests for TTS and video generation handlers."""
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
    session = (
        _FakeSession()
        .add(_FakeResp(status=200, body=json.dumps(_make_video_job("queued")).encode()))
        .add(_FakeResp(status=200, body=json.dumps(_make_video_job("in_progress")).encode()))
        .add(_FakeResp(status=200, body=json.dumps({**_make_video_job("succeeded")}).encode()))
        .add(_FakeResp(status=200, body=b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32))
    )
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
    assert result["status"] == "succeeded"
    assert "b64_json" in result


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
async def test_video_zai_create_polls_until_success_and_inlines_b64():
    """Create → poll → fetch MP4 from signed URL → base64 inlined under b64_json."""
    h = VideoHandler({})
    session = _FakeSession()
    session.add(_FakeResp(200, json.dumps({"id": "vid-1"}).encode()))
    # Polls return PROCESSING twice, then SUCCESS with a video URL.
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
    # Final content fetch from the signed URL.
    session.add(_FakeResp(200, b"MP4DATA"))

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
    # Two PROCESSING polls + SUCCESS poll + final content fetch
    urls = [u for (u, _) in captured["gets"]]
    assert urls[0] == "https://api.z.ai/api/paas/v4/async-result/vid-1"
    assert urls[1] == "https://api.z.ai/api/paas/v4/async-result/vid-1"
    assert urls[2] == "https://api.z.ai/api/paas/v4/async-result/vid-1"
    assert urls[3] == "https://signed.test/out.mp4"
    # SUCCESS poll carries the Authorization header.
    for u, h_ in captured["gets"][:3]:
        assert h_["Authorization"] == "Bearer zai-key"
    # b64_json should be the base64 of MP4DATA.
    import base64
    assert base64.b64decode(result["b64_json"]) == b"MP4DATA"


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

