"""Tests for the capability registry.

These exercise the discovery probes end-to-end against in-memory aiohttp
fakes rather than live providers, so they're deterministic and run offline.
The probes themselves are still *production code*; the fakes only substitute
for HTTP.
"""
import asyncio
import json
import logging
from typing import Any, Callable, Optional

import pytest

from tusker_gateway.providers.capabilities import (
    Capability,
    CapabilityEntry,
    CapabilitiesRegistry,
    capabilities_refresh_loop,
    discover_openrouter,
    normalise_model_for_lookup,
)


# ---------- Fixtures ----------


class _FakeContent:
    """Fake aiohttp response.content that yields a single buffer."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_any(self):
        if self._body:
            yield self._body


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", headers: Optional[dict] = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self) -> bytes:
        return self._body

    async def json(self) -> Any:
        return json.loads(self._body.decode())

    async def text(self) -> str:
        return self._body.decode("utf-8", "replace")


class _FakeSession:
    """Routes ``GET``/``POST`` to a handler based on URL substring.

    Handlers return a ``(status, body, headers)`` triple. Anything not
    matched falls back to a 404 so a missing fixture fails loudly.
    """

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Callable]] = []

    def route(self, method: str, url_substring: str, handler: Callable) -> None:
        self.routes.append((method.upper(), url_substring, handler))

    def _match(self, method: str, url: str):
        for m, s, h in self.routes:
            if m == method.upper() and s in url:
                return h
        return None

    def get(self, *args, **kwargs):
        url = args[0] if args else kwargs.get("url", "")
        h = self._match("GET", url)
        if h is None:
            return _FakeResponse(404, b"no-match")
        status, body, headers = h(url, kwargs.get("headers", {}))
        return _FakeResponse(status, body, headers)

    def post(self, *args, **kwargs):
        url = args[0] if args else kwargs.get("url", "")
        h = self._match("POST", url)
        if h is None:
            return _FakeResponse(404, b"no-match")
        json_payload = kwargs.get("json", {})
        status, body, headers = h(url, json_payload)
        return _FakeResponse(status, body, headers)


# ---------- OpenRouter ----------


@pytest.mark.asyncio
async def test_discover_openrouter_picks_image_output_modalities():
    catalog = {
        "data": [
            {
                "id": "google/gemini-3-pro-image:free",
                "architecture": {
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text", "image"],
                },
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "openai/gpt-4o",
                "architecture": {
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                },
            },
            {
                "id": "openai/gpt-audio",
                "architecture": {
                    "input_modalities": ["text", "audio"],
                    "output_modalities": ["text", "audio"],
                },
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]
    }
    session = _FakeSession()
    session.route("GET", "openrouter.ai/api/v1/models",
                  lambda url, h: (200, json.dumps(catalog).encode(), {}))
    entries = await discover_openrouter(session, "sk-test")
    by_model = {e.model for e in entries}
    assert "google/gemini-3-pro-image:free" in by_model
    assert "openai/gpt-4o" not in by_model
    # audio model is NOT registered as a TTS capability (no OR speech surface yet)
    assert "openai/gpt-audio" not in by_model

@pytest.mark.asyncio
async def test_discover_openrouter_picks_dedicated_video_models():
    session = _FakeSession()
    session.route("GET", "/api/v1/models", lambda *_: (200, b'{"data":[]}', {}))
    session.route("GET", "/api/v1/images/models", lambda *_: (200, b'{"data":[]}', {}))
    session.route(
        "GET",
        "/api/v1/videos/models",
        lambda *_: (200, b'{"data":[{"id":"google/veo-3.1:free","pricing":{"prompt":"0","completion":"0"}}]}', {}),
    )

    entries = await discover_openrouter(session, "sk-test")

    assert any(
        entry.model == "google/veo-3.1:free"
        and entry.capability == Capability.VIDEO_GENERATIONS
        for entry in entries
    )


@pytest.mark.asyncio
async def test_discover_openrouter_filters_paid_and_unknown_price_media():
    catalog = {
        "data": [
            {
                "id": "free/image:free",
                "architecture": {"output_modalities": ["image"]},
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "paid/image",
                "architecture": {"output_modalities": ["image"]},
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            },
            {
                "id": "unknown/image",
                "architecture": {"output_modalities": ["image"]},
            },
        ]
    }
    session = _FakeSession()
    session.route(
        "GET",
        "/api/v1/models",
        lambda *_: (200, json.dumps(catalog).encode(), {}),
    )
    session.route(
        "GET",
        "/api/v1/images/models",
        lambda *_: (
            200,
            json.dumps(
                {
                    "data": [
                        {
                            "id": "free/dedicated-image:free",
                            "pricing": {"prompt": "0", "completion": "0"},
                        },
                        {
                            "id": "paid/dedicated-image",
                            "pricing": {"prompt": "0.1", "completion": "0"},
                        },
                    ]
                }
            ).encode(),
            {},
        ),
    )
    session.route(
        "GET",
        "/api/v1/videos/models",
        lambda *_: (200, b'{"data":[]}', {}),
    )

    entries = await discover_openrouter(session, "sk-test")

    assert {entry.model for entry in entries} == {
        "free/image:free",
        "free/dedicated-image:free",
    }

@pytest.mark.asyncio
async def test_discover_openrouter_returns_empty_when_no_key():
    entries = await discover_openrouter(_FakeSession(), None)
    assert entries == []


@pytest.mark.asyncio
async def test_discover_openrouter_swallows_http_error():
    session = _FakeSession()
    session.route("GET", "openrouter.ai", lambda *_: (503, b"down", {}))
    entries = await discover_openrouter(session, "sk-test")
    assert entries == []


# ---------- Lookup / normalisation ----------


@pytest.mark.asyncio
async def test_registry_resolves_provider_for_model():
    reg = CapabilitiesRegistry()
    reg.snapshot.capabilities[Capability.IMAGE_GENERATIONS].append(
        CapabilityEntry(
            provider="openrouter",
            model="google/gemini-3-pro-image",
            capability=Capability.IMAGE_GENERATIONS,
        )
    )
    entry = reg.snapshot.lookup(Capability.IMAGE_GENERATIONS, "google/gemini-3-pro-image")
    assert entry is not None
    assert entry.provider == "openrouter"


def test_normalise_model_for_lookup_handles_provider_prefix():
    assert normalise_model_for_lookup("openai::gpt-image-1") == "gpt-image-1"
    # 'openrouter/google/gemini-...' is the canonical prefix form. After
    # stripping the openrouter/ part we land back on the upstream slug.
    assert normalise_model_for_lookup("google/gemini-3-pro-image") == "google/gemini-3-pro-image"
    assert normalise_model_for_lookup("gpt-image-2") == "gpt-image-2"


def test_normalise_model_for_lookup_no_change_for_bare_slug():
    assert normalise_model_for_lookup("cogView-4-250304") == "cogView-4-250304"
    assert normalise_model_for_lookup("cogvideox-3") == "cogvideox-3"
    assert normalise_model_for_lookup("") == ""


# ---------- Capability enum ----------


def test_capability_enum_values_are_stable():
    assert Capability.IMAGE_GENERATIONS.value == "image_generations"
    assert Capability.IMAGE_EDITS.value == "image_edits"
    assert Capability.IMAGE_VARIATIONS.value == "image_variations"
    assert Capability.TTS_SPEECH.value == "tts_speech"
    assert Capability.VIDEO_GENERATIONS.value == "video_generations"


# ---------- Refresh loop ----------


@pytest.mark.asyncio
async def test_capabilities_refresh_loop_runs_initial_refresh():
    reg = CapabilitiesRegistry()
    session = _FakeSession()
    session.route("GET", "openrouter.ai/api/v1/models",
                  lambda url, h: (200, b'{"data":[]}', {}))
    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    task = asyncio.create_task(capabilities_refresh_loop(reg, session, 60, stop))
    await stop_soon()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_capabilities_refresh_loop_survives_one_failure_and_recovers():
    """OpenRouter works even when Z.AI raises."""
    reg = CapabilitiesRegistry(provider_keys={"openrouter": "sk-test", "zai": "zai-test"})
    session = _FakeSession()
    session.route("GET", "openrouter.ai/api/v1/models",
                  lambda url, h: (200, json.dumps({"data": [
                      {
                          "id": "openrouter-test/img:free",
                          "architecture": {"output_modalities": ["image"]},
                          "pricing": {"prompt": "0", "completion": "0"},
                      }
                  ]}).encode(), {}))

    def _fail(url, body):
        raise RuntimeError("conn refused")
    session.route("POST", "api.z.ai", _fail)

    snap = await reg.refresh(session)
    images = snap.capabilities[Capability.IMAGE_GENERATIONS]
    # openrouter model shows up
    assert any(e.provider == "openrouter" for e in images)
    # Z.AI swallowed every probe error at slug level — so its snapshot is
    # empty, not poisoned. refresh() returned cleanly.
    zai_images = [e for e in images if e.provider == "zai"]
    assert zai_images == []
    # snapshot is usable
    assert isinstance(snap.errors, list)

@pytest.mark.asyncio
async def test_capabilities_refresh_loop_swallows_probe_errors():
    """Per-slug probe failures are debug-logged but do NOT poison the snapshot.

    Individual slugs may 404/410 (model doesn't exist for that provider) or
    fail to connect; those are expected misses. The registry surfaces only
    the *currently available* capabilities, not transient provider health.
    """
    reg = CapabilitiesRegistry(provider_keys={"zai": "bad-key"})
    session = _FakeSession()

    def _fail(url, _):
        raise RuntimeError("conn refused")
    session.route("POST", "api.z.ai", _fail)

    snap = await reg.refresh(session)
    # refresh() returned without raising; snapshot is empty but valid
    assert all(len(v) == 0 for v in snap.capabilities.values())
    assert snap.errors == []


# ---------- Integration: handler + registry ----------


@pytest.mark.asyncio
async def test_image_handler_consults_registry_when_present():
    """A registered provider should override the heuristic default."""
    from tusker_gateway.providers.image_generation import ImageGenerationHandler

    reg = CapabilitiesRegistry()
    reg.snapshot.capabilities[Capability.IMAGE_GENERATIONS].append(
        CapabilityEntry(
            provider="openrouter",
            model="google/gemini-3-pro-image",
            capability=Capability.IMAGE_GENERATIONS,
        )
    )
    h = ImageGenerationHandler({})
    # Heuristic alone would pick 'google' (contains 'gemini').
    # The registry says 'openrouter'. Registry wins when populated.
    provider = h.get_provider_for_image_request(
        "google/gemini-3-pro-image",
        "/v1/images/generations",
        capability_registry=reg,
    )
    assert provider == "openrouter"


@pytest.mark.asyncio
async def test_image_handler_falls_back_when_registry_empty():
    """With an empty registry, heuristics decide."""
    from tusker_gateway.providers.image_generation import ImageGenerationHandler

    reg = CapabilitiesRegistry()  # empty snapshot
    h = ImageGenerationHandler({})
    provider = h.get_provider_for_image_request(
        "gemini-2.5-flash-image",  # no slash → heuristic picks 'google'
        "/v1/images/generations",
        capability_registry=reg,
    )
    assert provider == "google"


@pytest.mark.asyncio
async def test_image_handler_falls_back_when_model_not_in_registry():
    """An unknown model id is routed by heuristics, not 404."""
    from tusker_gateway.providers.image_generation import ImageGenerationHandler

    reg = CapabilitiesRegistry()
    reg.snapshot.capabilities[Capability.IMAGE_GENERATIONS].append(
        CapabilityEntry(provider="zai", model="cogView-4-250304", capability=Capability.IMAGE_GENERATIONS)
    )
    h = ImageGenerationHandler({})
    # 'dall-e-3' isn't in the registry but heuristics know it's OpenAI.
    provider = h.get_provider_for_image_request(
        "dall-e-3",
        "/v1/images/generations",
        capability_registry=reg,
    )
    assert provider == "openai"
