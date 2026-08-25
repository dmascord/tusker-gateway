"""Tests for image generation provider routing and Codex OAuth pathway."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tusker_gateway.errors import GatewayError
from tusker_gateway.providers.image_generation import (
    ImageGenerationHandler,
    _map_model_to_codex,
)


def _make_config():
    return {
        "provider_api_keys": {
            "openai": None,  # no direct OpenAI key -> must use Codex pathway
        },
        "codex_credentials": [
            {
                "access_token": "test-access-token",
                "refresh_token": "test-refresh-token",
                "expires_at_ms": 9999999999999,
                "label": "test-codex",
            }
        ],
    }



def test_map_model_to_codex_gpt_image():
    # Codex/ChatGPT currently only supports gpt-image-2.
    assert _map_model_to_codex("gpt-image-1") == "gpt-image-2"
    assert _map_model_to_codex("gpt-image-2") == "gpt-image-2"
    assert _map_model_to_codex("GPT-IMAGE-MINI") == "gpt-image-2"


def test_map_model_to_codex_dall_e():
    # DALL-E names route to gpt-image-2 since that's the only Codex slug.
    assert _map_model_to_codex("dall-e-3") == "gpt-image-2"
    assert _map_model_to_codex("dall-e-2") == "gpt-image-2"


def test_map_model_to_codex_passthrough():
    assert _map_model_to_codex("custom-model") == "custom-model"


def test_map_model_to_codex_empty_and_auto():
    assert _map_model_to_codex("") == "gpt-image-2"
    assert _map_model_to_codex("auto") == "gpt-image-2"



def test_get_provider_for_image_request_openai():
    h = ImageGenerationHandler({})
    assert h.get_provider_for_image_request("gpt-image-1", "/v1/images/generations") == "openai"
    assert h.get_provider_for_image_request("dall-e-3", "/v1/images/generations") == "openai"


def test_get_provider_for_image_request_openrouter():
    h = ImageGenerationHandler({})
    assert h.get_provider_for_image_request("openrouter::openai/gpt-image-1", "/v1/images/generations") == "openrouter"
    assert h.get_provider_for_image_request("openrouter/google/gemini-2.5-flash-image", "/v1/images/generations") == "openrouter"
    assert h.get_provider_for_image_request("openai/gpt-image-1", "/v1/images/generations") == "openai"


def test_get_provider_for_image_request_google():
    h = ImageGenerationHandler({})
    assert h.get_provider_for_image_request("gemini-2.5-flash-image", "/v1/images/generations") == "google"
    assert h.get_provider_for_image_request("imagen-3.0-generate-002", "/v1/images/generations") == "google"


def test_get_provider_for_image_request_anthropic_is_rejected():
    h = ImageGenerationHandler({})
    with pytest.raises(GatewayError) as excinfo:
        h.get_provider_for_image_request("claude-sonnet-4", "/v1/images/generations")
    assert excinfo.value.code == "unsupported_model"


def test_get_provider_for_image_request_zai():
    """Z.ai's CogView and GLM-Image models route to the zai provider."""
    h = ImageGenerationHandler({})
    assert h.get_provider_for_image_request("cogview-4-250304", "/v1/images/generations") == "zai"
    assert h.get_provider_for_image_request("glm-image", "/v1/images/generations") == "zai"


def test_is_image_generation_request_recognises_zai_models():
    """Model slug alone (cogview-*, glm-image) is enough to flag as image gen."""
    h = ImageGenerationHandler({})
    assert h.is_image_generation_request("POST", "/v1/images/generations", "cogview-4-250304")
    assert h.is_image_generation_request("POST", "/v1/images/generations", "glm-image")


@pytest.mark.asyncio
async def test_zai_dispatches_to_call_zai():
    """handle_request routes cogview-* / glm-image to _call_zai."""
    h = ImageGenerationHandler({})
    captured = {}

    async def fake_call_zai(self, model, path, body, api_key, extra_headers):
        captured["model"] = model
        captured["api_key"] = api_key
        return {"created": 99, "data": [{"url": "https://example.test/x.png"}]}

    with patch.object(ImageGenerationHandler, "_call_zai", new=fake_call_zai):
        result = await h.handle_request(
            model="cogview-4-250304",
            path="/v1/images/generations",
            body={"model": "cogview-4-250304", "prompt": "a dragon"},
            api_key="zai-key-abc",
        )

    assert result["data"][0]["url"] == "https://example.test/x.png"
    assert captured["model"] == "cogview-4-250304"
    assert captured["api_key"] == "zai-key-abc"


@pytest.mark.asyncio
async def test_zai_call_zai_raises_without_api_key():
    h = ImageGenerationHandler({})
    with pytest.raises(GatewayError) as excinfo:
        await h._call_zai(
            model="glm-image",
            path="/v1/images/generations",
            body={"prompt": "x"},
            api_key=None,
            extra_headers=None,
        )
    assert excinfo.value.code == "missing_api_key"


@pytest.mark.asyncio
async def test_zai_call_zai_posts_to_paas_endpoint():
    """Verify the upstream URL, headers, and request payload shape."""
    captured: dict = {}

    class _FakeResp:
        status = 200

        async def text(self):
            return json.dumps({"created": 1, "data": [{"url": "https://up.test/img.png"}]})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        def post(self, url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    h = ImageGenerationHandler({})
    with patch(
        "tusker_gateway.providers.image_generation.aiohttp.ClientSession",
        return_value=_FakeSession(),
    ):
        result = await h._call_zai(
            model="cogview-4-250304",
            path="/v1/images/generations",
            body={"prompt": "a dragon", "size": "1024x1024"},
            api_key="zai-key",
            extra_headers=None,
        )

    assert captured["url"] == "https://api.z.ai/api/paas/v4/images/generations"
    assert captured["headers"]["Authorization"] == "Bearer zai-key"
    assert captured["body"]["model"] == "cogView-4-250304"  # canonical rewrite
    assert captured["body"]["prompt"] == "a dragon"
    assert captured["body"]["size"] == "1024x1024"
    assert result["data"][0]["url"] == "https://up.test/img.png"

@pytest.mark.asyncio
async def test_openai_falls_back_to_codex_when_no_api_key():
    h = ImageGenerationHandler(_make_config())
    fake_rotator = MagicMock()
    fake_rotator.get_token = AsyncMock(return_value="rotator-token")

    captured = {}

    async def fake_call_openai_codex(self, model, path, body, codex_rotator, extra_headers):
        captured["model"] = model
        captured["rotator"] = codex_rotator
        return {"created": 1, "data": [{"b64_json": "AAA"}]}

    with patch.object(
        ImageGenerationHandler, "_call_openai_codex", new=fake_call_openai_codex
    ):
        result = await h.handle_request(
            model="gpt-image-1",
            path="/v1/images/generations",
            body={"model": "gpt-image-1", "prompt": "A mountain"},
            api_key=None,
            codex_rotator=fake_rotator,
        )

    assert result["created"] == 1
    assert captured["model"] == "gpt-image-1"
    assert captured["rotator"] is fake_rotator


@pytest.mark.asyncio
async def test_openai_uses_direct_key_when_present():
    h = ImageGenerationHandler(_make_config())
    captured = {}

    async def fake_call_openai_direct(self, model, path, body, api_key, extra_headers):
        captured["api_key"] = api_key
        return {"created": 2, "data": []}

    with patch.object(
        ImageGenerationHandler, "_call_openai_direct", new=fake_call_openai_direct
    ):
        result = await h.handle_request(
            model="gpt-image-1",
            path="/v1/images/generations",
            body={"model": "gpt-image-1", "prompt": "x"},
            api_key="sk-direct-key",
        )

    assert result["created"] == 2
    assert captured["api_key"] == "sk-direct-key"


@pytest.mark.asyncio
async def test_openai_raises_when_no_credentials():
    h = ImageGenerationHandler({})

    with pytest.raises(GatewayError) as excinfo:
        await h.handle_request(
            model="gpt-image-1",
            path="/v1/images/generations",
            body={"prompt": "x"},
            api_key=None,
            codex_rotator=None,
        )

    assert excinfo.value.code == "missing_api_key"


class _FakeContent:
    """Async iterable that returns one chunk of bytes then stops."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self._done = False

    async def iter_any(self):
        if not self._done:
            self._done = True
            yield self.payload


class _FakeResp:
    def __init__(self, *, status: int = 200, body: bytes = b"", err_text: str = ""):
        self.status = status
        self._body = body
        self._err_text = err_text
        self._content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def content(self):
        return self._content

    async def text(self):
        return self._err_text


class _FakeSession:
    def __init__(self, *args, **kwargs):
        self._resp: _FakeResp | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def set_response(self, resp: _FakeResp) -> None:
        self._resp = resp

    def post(self, url, headers=None, json=None):
        assert self._resp is not None, "FakeSession.post called without set_response"
        return self._resp


def _sse(*events: dict) -> bytes:
    """Build an SSE byte payload from a list of event dicts."""
    out = b""
    for ev in events:
        import json as _json
        out += b"data: " + _json.dumps(ev).encode() + b"\n\n"
    out += b"data: [DONE]\n\n"
    return out


@pytest.mark.asyncio
async def test_call_openai_codex_parses_sse_response():
    """Verify the Codex SSE parser extracts base64 images from image_generation_call items."""
    h = ImageGenerationHandler(_make_config())
    fake_rotator = MagicMock()
    fake_rotator.get_token = AsyncMock(return_value="test-token")

    body = _sse(
        {
            "type": "response.image_generation_call.completed",
            "image_generation_call": {"result": "BASE64PNG", "revised_prompt": "a mountain peak"},
        },
        {"type": "response.completed", "response": {"output": []}},
    )

    session = _FakeSession()
    session.set_response(_FakeResp(status=200, body=body))

    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h._call_openai_codex(
            model="gpt-image-1",
            path="/v1/images/generations",
            body={"model": "gpt-image-1", "prompt": "A mountain peak", "size": "1024x1024", "n": 1},
            codex_rotator=fake_rotator,
            extra_headers=None,
        )

    assert "created" in result
    # Multiple images is the only case where revised_prompt is exposed
    # (OpenAI's API returns revised_prompt only when n > 1).
    assert result["data"][0]["b64_json"] == "BASE64PNG"


@pytest.mark.asyncio
async def test_call_openai_codex_raises_on_no_images():
    h = ImageGenerationHandler(_make_config())
    fake_rotator = MagicMock()
    fake_rotator.get_token = AsyncMock(return_value="test-token")

    body = _sse({"type": "response.created", "response": {"output": []}})
    session = _FakeSession()
    session.set_response(_FakeResp(status=200, body=body))

    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        with pytest.raises(GatewayError) as excinfo:
            await h._call_openai_codex(
                model="gpt-image-1",
                path="/v1/images/generations",
                body={"prompt": "x"},
                codex_rotator=fake_rotator,
                extra_headers=None,
            )

    assert "no images" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_call_openai_codex_handles_error_status():
    h = ImageGenerationHandler(_make_config())
    fake_rotator = MagicMock()
    fake_rotator.get_token = AsyncMock(return_value="test-token")

    session = _FakeSession()
    session.set_response(_FakeResp(status=429, err_text='{"error": {"message": "rate limited"}}'))

    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        with pytest.raises(GatewayError) as excinfo:
            await h._call_openai_codex(
                model="gpt-image-1",
                path="/v1/images/generations",
                body={"prompt": "x"},
                codex_rotator=fake_rotator,
                extra_headers=None,
            )

    assert excinfo.value.code == "upstream_error"
    assert "429" in str(excinfo.value)


@pytest.mark.asyncio
async def test_call_openai_codex_pulls_multiple_images_from_response_completed():
    """The response.completed event may carry the full output list with images."""
    h = ImageGenerationHandler(_make_config())
    fake_rotator = MagicMock()
    fake_rotator.get_token = AsyncMock(return_value="test-token")

    body = _sse(
        {
            "type": "response.completed",
            "response": {
                "output": [
                    {"type": "image_generation_call", "result": "PNG1", "revised_prompt": "first"},
                    {"type": "image_generation_call", "result": "PNG2", "revised_prompt": "second"},
                ]
            },
        }
    )
    session = _FakeSession()
    session.set_response(_FakeResp(status=200, body=body))



@pytest.mark.asyncio
async def test_call_openai_codex_extracts_partial_image_b64():
    """Verify the parser extracts partial_image_b64 from partial_image events."""
    h = ImageGenerationHandler(_make_config())
    fake_rotator = MagicMock()
    fake_rotator.get_token = AsyncMock(return_value="test-token")

    body = _sse(
        {
            "type": "response.image_generation_call.in_progress",
            "item_id": "ig_x",
            "output_index": 0,
        },
        {
            "type": "response.image_generation_call.generating",
            "item_id": "ig_x",
            "output_index": 0,
        },
        {
            "type": "response.image_generation_call.partial_image",
            "item_id": "ig_x",
            "output_index": 0,
            "partial_image_b64": "PARTIAL_BASE64",
        },
        {"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": "FINAL_BASE64"}},
        {"type": "response.completed", "response": {"output": []}},
    )
    session = _FakeSession()
    session.set_response(_FakeResp(status=200, body=body))

    with patch("aiohttp.ClientSession", lambda *a, **kw: session):
        result = await h._call_openai_codex(
            model="gpt-image-1",
            path="/v1/images/generations",
            body={"prompt": "x"},
            codex_rotator=fake_rotator,
            extra_headers=None,
        )

    # The final image (from output_item.done) overrides the partial preview.
    assert result["data"][-1]["b64_json"] == "FINAL_BASE64"


@pytest.mark.asyncio
async def test_zai_dispatch_canonicalizes_lowercase_cogview_slug():
    """Lowercase 'cogview-4-250304' is rewritten to 'cogView-4-250304' before
    posting to Z.AI's PaaS API. The capital-V form is what the upstream
    accepts.
    """
    from tusker_gateway.providers.image_generation import ImageGenerationHandler

    captured: dict = {}

    class _FakeResp:
        def __init__(self):
            self.status = 200
            self._b = b'{"created":1,"data":[{"url":"https://x/a.png"}]}'
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def read(self):
            return self._b
        async def text(self):
            return self._b.decode()
        async def json(self):
            return json.loads(self._b.decode())

    class _FakeSession:
        def post(self, url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json", {})
            return _FakeResp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    h = ImageGenerationHandler({})
    import aiohttp
    orig = aiohttp.ClientSession
    aiohttp.ClientSession = lambda *a, **k: _FakeSession()
    try:
        result = await h._call_zai(
            model="cogview-4-250304",
            path="/v1/images/generations",
            body={"prompt": "a cat"},
            api_key="zai-test",
            extra_headers=None,
        )
    finally:
        aiohttp.ClientSession = orig

    assert captured["json"]["model"] == "cogView-4-250304"
    assert result["data"][0]["url"] == "https://x/a.png"


@pytest.mark.asyncio
async def test_zai_dispatch_passes_unknown_slugs_through_unchanged():
    """Models not in the canonical map (e.g., glm-image) are not modified."""
    from tusker_gateway.providers.image_generation import ImageGenerationHandler

    captured: dict = {}

    class _FakeResp:
        def __init__(self):
            self.status = 200
            self._b = b'{"created":1,"data":[{"url":"https://x/a.png"}]}'
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def read(self):
            return self._b
        async def text(self):
            return self._b.decode()
        async def json(self):
            return json.loads(self._b.decode())

    class _FakeSession:
        def post(self, url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json", {})
            return _FakeResp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    h = ImageGenerationHandler({})
    import aiohttp
    orig = aiohttp.ClientSession
    aiohttp.ClientSession = lambda *a, **k: _FakeSession()
    try:
        await h._call_zai(
            model="glm-image",
            path="/v1/images/generations",
            body={"prompt": "a cat"},
            api_key="zai-test",
            extra_headers=None,
        )
    finally:
        aiohttp.ClientSession = orig

    assert captured["json"]["model"] == "glm-image"


@pytest.mark.asyncio
async def test_zai_routes_image_and_video_via_registry():
    """End-to-end routing check: a populated registry steers both image
    generation (cogview-4) and video generation (cogvideox-3) to Z.AI
    without falling back to heuristic string matching.
    """
    from tusker_gateway.providers.capabilities import (
        Capability,
        CapabilityEntry,
        CapabilitiesRegistry,
    )
    from tusker_gateway.providers.image_generation import ImageGenerationHandler
    from tusker_gateway.providers.video import VideoHandler

    reg = CapabilitiesRegistry()
    reg.snapshot.capabilities[Capability.IMAGE_GENERATIONS].append(
        CapabilityEntry(provider="zai", model="cogView-4-250304", capability=Capability.IMAGE_GENERATIONS)
    )
    reg.snapshot.capabilities[Capability.VIDEO_GENERATIONS].append(
        CapabilityEntry(provider="zai", model="cogvideox-3", capability=Capability.VIDEO_GENERATIONS)
    )

    img_handler = ImageGenerationHandler({})
    vid_handler = VideoHandler({})

    # Image request: lowercase slug still routes correctly via registry.
    assert img_handler.get_provider_for_image_request(
        "cogview-4-250304", "/v1/images/generations", capability_registry=reg
    ) == "zai"
    # Video request: pure registry-based decision, no slash in slug.
    assert vid_handler.get_provider_for_video_request(
        "cogvideox-3", capability_registry=reg
    ) == "zai"
