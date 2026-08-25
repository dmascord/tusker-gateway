"""Capability registry for image / TTS / video endpoints.

This module answers the question: "given a model slug, which provider + API
shape should I use to satisfy ``/v1/images/generations``, ``/v1/audio/speech``,
or ``/v1/videos``?". It replaces the per-handler ``IMAGE_GEN_MODELS``/hardcoded
``cogView-4-250304``/etc. lists with a runtime registry that is built from
provider catalogs and probed against the live API surface each refresh.

Design notes
------------
* A capability is one of {image_generations, image_edits, image_variations,
  tts_speech, video_generations}. Each capability maps to a set of
  ``(provider, model)`` tuples the gateway knows it can dispatch to.
* Refresh is per-provider; results are merged. Failures of one provider
  don't poison the others.
* The handlers consult the registry with ``resolve_model()`` instead of using
  a static lookup table. Adding a new image-capable upstream becomes "wait
  for next refresh" rather than "edit this file".
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

import aiohttp
from tusker_gateway.catalog import is_free_openrouter_model

logger = logging.getLogger(__name__)


class Capability(str, enum.Enum):
    IMAGE_GENERATIONS = "image_generations"
    IMAGE_EDITS = "image_edits"
    IMAGE_VARIATIONS = "image_variations"
    TTS_SPEECH = "tts_speech"
    VIDEO_GENERATIONS = "video_generations"


@dataclass(frozen=True)
class CapabilityEntry:
    """A single (provider, model) tuple known to satisfy a capability."""

    provider: str     # tusker-gateway provider key (e.g. 'openrouter')
    model: str        # upstream model id (e.g. 'google/gemini-3-pro-image')
    capability: Capability
    # Hint to the handler about cost (best-effort; None when unknown).
    # For Z.AI's per-image billing we use a sentinel so the handler can
    # distinguish "per-image cost" from "per-token cost".
    cost_input: float | None = None
    cost_output: float | None = None


@dataclass
class CapabilitySnapshot:
    """Result of one refresh cycle."""

    capabilities: dict[Capability, list[CapabilityEntry]] = field(
        default_factory=lambda: {c: [] for c in Capability}
    )
    errors: list[str] = field(default_factory=list)

    def lookup(self, capability: Capability, model: str) -> CapabilityEntry | None:
        """Return the first entry matching ``(capability, model)`` or None.

        When the same upstream id is published by more than one provider
        (e.g. ``google/gemini-3-pro-image`` is listed on OpenRouter and
        Google alike), this returns the first one in refresh order. The
        refresh order is deterministic and favours the cheaper / more local
        path first.
        """
        for entry in self.capabilities[capability]:
            if entry.model == model:
                return entry
        return None

    def providers_for_model(self, capability: Capability, model: str) -> list[str]:
        return [e.provider for e in self.capabilities[capability] if e.model == model]


class CapabilitiesRegistry:
    """In-memory registry of (provider, model) per capability.

    Construct once at startup, refresh via :meth:`refresh` on a schedule
    (see :func:`capabilities_refresh_loop`). Handlers consult the latest
    :attr:`snapshot` to route model slugs to providers.
    """

    def __init__(
        self,
        provider_keys: dict[str, str] | None = None,
        codex_rotator: Any | None = None,
    ) -> None:
        self.provider_keys: dict[str, str] = dict(provider_keys or {})
        self.codex_rotator = codex_rotator
        self.snapshot = CapabilitySnapshot()
        self._lock = asyncio.Lock()

    async def refresh(self, session: aiohttp.ClientSession) -> CapabilitySnapshot:
        """Probe every provider, replace the snapshot atomically."""
        async with self._lock:
            providers = [
                ("openrouter", discover_openrouter(session, self.provider_keys.get("openrouter"))),
                ("openai", _discover_openai_via_probe(session, self.provider_keys.get("openai"))),
                ("codex", _discover_codex_capability(self.codex_rotator)),
                ("zai", _discover_zai(self.provider_keys.get("zai") or self.provider_keys.get("glm"), session)),
                ("xiaomi", _discover_xiaomi(self.provider_keys.get("xiaomi"), session)),
                ("google", _discover_google(self.provider_keys.get("google") or self.provider_keys.get("gemini"), session)),
            ]

            results = await asyncio.gather(
                *[_safe(name, coro) for name, coro in providers],
                return_exceptions=False,
            )
            merged = CapabilitySnapshot()
            for provider_name, entries in results:  # type: ignore[misc]
                if isinstance(entries, Exception):
                    merged.errors.append(f"{provider_name}: {entries}")
                    continue
                for entry in entries:
                    merged.capabilities[entry.capability].append(entry)

            self.snapshot = merged
            total = sum(len(v) for v in merged.capabilities.values())
            logger.info(
                "capabilities refreshed: %s (errors=%d)",
                {c.value: len(v) for c, v in merged.capabilities.items()},
                len(merged.errors),
            )
            return merged

async def _safe(
    provider_name: str,
    coro_or_entries: list[CapabilityEntry] | Awaitable[list[CapabilityEntry]],
) -> tuple[str, list[CapabilityEntry] | Exception]:
    """Run an entry-discovery callable and label its exception with the provider name."""
    try:
        if asyncio.iscoroutine(coro_or_entries):
            result = await coro_or_entries  # type: ignore[assignment]
        else:
            result = coro_or_entries  # type: ignore[assignment]
        return provider_name, list(result)
    except Exception as exc:  # noqa: BLE001
        return provider_name, exc


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------


async def discover_openrouter(
    session: aiohttp.ClientSession,
    api_key: str | None,
) -> list[CapabilityEntry]:
    """Discover only explicitly free OpenRouter image and video models."""
    if not api_key:
        return []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "tusker-gateway/capabilities",
    }
    out: list[CapabilityEntry] = []
    seen: set[tuple[Capability, str]] = set()

    async def fetch_models(url: str) -> list[dict[str, Any]]:
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "openrouter capability probe %s: http %s", url, resp.status
                    )
                    return []
                data = await resp.json()
        except Exception as exc:
            logger.warning("openrouter capability probe %s failed: %s", url, exc)
            return []
        models = (data or {}).get("data", []) if isinstance(data, dict) else []
        return [
            model
            for model in models
            if isinstance(model, dict) and is_free_openrouter_model(model)
        ]

    general_models = await fetch_models("https://openrouter.ai/api/v1/models")
    for model in general_models:
        slug = model.get("id")
        output_modalities = (model.get("architecture") or {}).get(
            "output_modalities"
        ) or []
        if not isinstance(slug, str) or not slug.strip():
            continue
        if isinstance(output_modalities, list) and "image" in output_modalities:
            seen.add((Capability.IMAGE_GENERATIONS, slug))
            out.append(
                CapabilityEntry(
                    provider="openrouter",
                    model=slug,
                    capability=Capability.IMAGE_GENERATIONS,
                )
            )

    for url, capability in (
        ("https://openrouter.ai/api/v1/images/models", Capability.IMAGE_GENERATIONS),
        ("https://openrouter.ai/api/v1/videos/models", Capability.VIDEO_GENERATIONS),
    ):
        for model in await fetch_models(url):
            slug = model.get("id")
            key = (capability, slug)
            if not isinstance(slug, str) or not slug.strip() or key in seen:
                continue
            seen.add(key)
            out.append(
                CapabilityEntry(
                    provider="openrouter", model=slug, capability=capability
                )
            )
    return out


# ---------------------------------------------------------------------------
# OpenAI (when a direct OPENAI_API_KEY is available)
# ---------------------------------------------------------------------------


# Static list per OpenAI's documented endpoint surface; we use it only when
# an OPENAI_API_KEY is configured and skip otherwise. Cost values are from
# OpenAI's pricing page as of 2026-08 and used purely for budget hints.
_OPENAI_STATIC_MODELS: dict[Capability, list[CapabilityEntry]] = {
    Capability.IMAGE_GENERATIONS: [
        CapabilityEntry(provider="openai", model="gpt-image-1", capability=Capability.IMAGE_GENERATIONS, cost_input=0.020, cost_output=0.020),
        CapabilityEntry(provider="openai", model="gpt-image-1-mini", capability=Capability.IMAGE_GENERATIONS, cost_input=0.005, cost_output=0.005),
        CapabilityEntry(provider="openai", model="dall-e-3", capability=Capability.IMAGE_GENERATIONS, cost_input=0.040, cost_output=0.040),
        CapabilityEntry(provider="openai", model="dall-e-2", capability=Capability.IMAGE_GENERATIONS, cost_input=0.020, cost_output=0.020),
    ],
    Capability.IMAGE_EDITS: [
        CapabilityEntry(provider="openai", model="gpt-image-1", capability=Capability.IMAGE_EDITS),
        CapabilityEntry(provider="openai", model="dall-e-2", capability=Capability.IMAGE_EDITS),
    ],
    Capability.IMAGE_VARIATIONS: [
        CapabilityEntry(provider="openai", model="dall-e-2", capability=Capability.IMAGE_VARIATIONS),
    ],
    Capability.TTS_SPEECH: [
        CapabilityEntry(provider="openai", model="tts-1", capability=Capability.TTS_SPEECH),
        CapabilityEntry(provider="openai", model="tts-1-hd", capability=Capability.TTS_SPEECH),
        CapabilityEntry(provider="openai", model="gpt-4o-mini-tts", capability=Capability.TTS_SPEECH),
    ],
    Capability.VIDEO_GENERATIONS: [
        CapabilityEntry(provider="openai", model="sora-2", capability=Capability.VIDEO_GENERATIONS),
    ],
}

async def _discover_openai_via_probe(
    session: aiohttp.ClientSession,
    api_key: str | None,
) -> list[CapabilityEntry]:
    """Verify the OpenAI key with a cheap ``/v1/models`` probe, then return the static table."""
    if not api_key:
        return []
    return await discover_openai_verified(session, api_key)


async def discover_openai_verified(
    session: aiohttp.ClientSession, api_key: str
) -> list[CapabilityEntry]:
    """Verify the OpenAI key with a cheap ``/v1/models`` probe, then return the static table."""
    try:
        async with session.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status >= 400:
                logger.warning("openai probe /v1/models: http %s", resp.status)
                return []
    except Exception as exc:
        logger.warning("openai probe failed: %s", exc)
        return []
    out: list[CapabilityEntry] = []
    for entries in _OPENAI_STATIC_MODELS.values():
        out.extend(entries)
    return out


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


# Static list per Codex Responses API; the Codex image_generation tool works
# without authentication specifics. We confirm capability with a minimal probe
# that does NOT actually generate an image.
_CODEX_STATIC_MODELS: list[CapabilityEntry] = [
    CapabilityEntry(provider="codex", model="gpt-image-1", capability=Capability.IMAGE_GENERATIONS, cost_input=0.020, cost_output=0.020),
    CapabilityEntry(provider="codex", model="gpt-image-1-mini", capability=Capability.IMAGE_GENERATIONS, cost_input=0.005, cost_output=0.005),
    CapabilityEntry(provider="codex", model="gpt-image-2", capability=Capability.IMAGE_GENERATIONS, cost_input=0.005, cost_output=0.005),
]


async def _discover_codex_capability(rotator: Any | None) -> list[CapabilityEntry]:
    """Codex advertises ``image_generation`` as a Responses tool; confirm by
    sending a 0-prompt probe and checking the response shape."""
    if rotator is None:
        return []
    # A lightweight probe — pull a token and POST a minimal payload with the
    # image_generation tool. We don't generate an image; we just want to see
    # the tool get accepted (or a structured 4xx with the known error code).
    try:
        token = await rotator.get_token()
    except Exception as exc:
        logger.info("codex capability probe: no token (%s)", exc)
        return []
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.0.0 (Tusker Gateway)",
    }
    payload = {
        "model": "gpt-5.5",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "probe"}]}],
        "tools": [{"type": "image_generation", "model": "gpt-image-1"}],
        "tool_choice": "auto",
        "stream": False,
        "store": False,
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        try:
            async with session.post(
                "https://chatgpt.com/backend-api/codex/responses",
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status >= 400:
                    logger.info("codex image_generation probe rejected: http %s", resp.status)
                    # 401 = no usable account token => capability unavailable.
                    # 4xx elsewhere = provider changed format; treat as unavailable.
                    return []
                await resp.read()
        except Exception as exc:
            logger.info("codex image_generation probe failed: %s", exc)
            return []
    return list(_CODEX_STATIC_MODELS)


# ---------------------------------------------------------------------------
# Z.AI
# ---------------------------------------------------------------------------


# Documented at https://docs.z.ai/llms.txt and the per-page references.
_ZAI_IMAGE_MODELS = [
    "cogView-4-250304",  # case-sensitive: docs use capital V
    "glm-image",
]
_ZAI_VIDEO_MODELS = [
    "cogvideox-3",
    "viduq1-text",
    "vidu2",
]


async def _discover_zai(api_key: str | None, session: aiohttp.ClientSession) -> list[CapabilityEntry]:
    """Probe Z.AI's per-capability endpoints with their documented slugs."""
    if not api_key:
        return []
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    out: list[CapabilityEntry] = []

    # Image generations — POST a probe; budget errors are fine; "Unknown Model" isn't.
    for slug in _ZAI_IMAGE_MODELS:
        try:
            async with session.post(
                "https://api.z.ai/api/paas/v4/images/generations",
                headers=headers,
                json={"model": slug, "prompt": "capability probe"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                body = (await r.read())[:200].decode("utf-8", "replace")
                if r.status == 200 or "balance" in body.lower():
                    out.append(CapabilityEntry(provider="zai", model=slug, capability=Capability.IMAGE_GENERATIONS))
                elif "Unknown Model" not in body and "1211" not in body:
                    # Some other failure (network, 5xx); don't blind-register.
                    logger.debug("zai image probe %s: status=%s body=%s", slug, r.status, body[:120])
                # else: model not accepted by Z.AI; skip silently
        except Exception as exc:
            logger.debug("zai image probe %s failed: %s", slug, exc)

    # Video generations — async. POST returns immediately with a job id
    # which is then polled via /videos/generations/<id>.
    for slug in _ZAI_VIDEO_MODELS:
        try:
            async with session.post(
                "https://api.z.ai/api/paas/v4/videos/generations",
                headers=headers,
                json={"model": slug, "prompt": "capability probe"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                body = (await r.read())[:200].decode("utf-8", "replace")
                if r.status == 200 or "balance" in body.lower():
                    out.append(CapabilityEntry(provider="zai", model=slug, capability=Capability.VIDEO_GENERATIONS))
                elif "Unknown Model" not in body and "1211" not in body:
                    logger.debug("zai video probe %s: status=%s body=%s", slug, r.status, body[:120])
        except Exception as exc:
            logger.debug("zai video probe %s failed: %s", slug, exc)

    # Z.AI does NOT publish a TTS model on its PaaS API yet (the open-source
    # GLM-TTS is self-hosted only). Don't register any TTS here.

    return out


# ---------------------------------------------------------------------------
# Google (Gemini native image generation)
# ---------------------------------------------------------------------------


async def _discover_google(
    api_key: str | None,
    session: aiohttp.ClientSession,
) -> list[CapabilityEntry]:
    """Discover native Gemini image and Veo video generation models."""
    if not api_key:
        return []
    headers = {"x-goog-api-key": api_key}
    try:
        async with session.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                logger.warning("google capability probe: http %s", response.status)
                return []
            data = await response.json()
    except Exception as exc:
        logger.warning("google capability probe failed: %s", exc)
        return []

    out: list[CapabilityEntry] = []
    for model in (data or {}).get("models", []):
        if not isinstance(model, dict):
            continue
        name = model.get("name")
        if not isinstance(name, str) or not name:
            continue
        model_id = name.removeprefix("models/")
        lower = model_id.lower()
        methods = model.get("supportedGenerationMethods") or []
        if "image" in lower and (
            "generateContent" in methods or "predict" in methods
        ):
            out.append(
                CapabilityEntry(
                    provider="google",
                    model=model_id,
                    capability=Capability.IMAGE_GENERATIONS,
                )
            )
        if lower.startswith("veo-") and "predictLongRunning" in methods:
            out.append(
                CapabilityEntry(
                    provider="google",
                    model=model_id,
                    capability=Capability.VIDEO_GENERATIONS,
                )
            )
    return out


# Xiaomi MiMo TTS
# ---------------------------------------------------------------------------


async def _discover_xiaomi(
    api_key: str | None,
    session: aiohttp.ClientSession,
) -> list[CapabilityEntry]:
    """Discover entitled Xiaomi TTS models from the regional model catalog."""
    if not api_key:
        return []
    url = "https://token-plan-sgp.xiaomimimo.com/v1/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "tusker-gateway/capabilities",
    }
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                body = (await response.read())[:200].decode("utf-8", "replace")
                logger.warning(
                    "xiaomi capability catalog: http %s body=%s",
                    response.status,
                    body,
                )
                return []
            data = await response.json()
    except Exception as exc:
        logger.warning("xiaomi capability catalog failed: %s", exc)
        return []

    entries: list[CapabilityEntry] = []
    seen: set[str] = set()
    for raw_model in (data or {}).get("data", []):
        if not isinstance(raw_model, dict):
            continue
        model = raw_model.get("id")
        if (
            not isinstance(model, str)
            or not model.lower().startswith("mimo-v2.5-tts")
            or model in seen
        ):
            continue
        seen.add(model)
        entries.append(
            CapabilityEntry(
                provider="xiaomi",
                model=model,
                capability=Capability.TTS_SPEECH,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Refresh loop
# ---------------------------------------------------------------------------


async def capabilities_refresh_loop(
    registry: CapabilitiesRegistry,
    session: aiohttp.ClientSession,
    interval_secs: float,
    stop_event: asyncio.Event,
    on_refresh: Callable[[], None] | None = None,
) -> None:
    """Refresh the capability registry on a cadence.

    First refresh runs immediately so the handlers have data on first
    request, then every ``interval_secs`` thereafter. Exceptions are
    logged and swallowed so transient upstream failures don't kill the
    loop.
    """
    try:
        await registry.refresh(session)
        if on_refresh is not None:
            on_refresh()
    except Exception as exc:
        logger.warning("initial capability refresh failed: %s", exc)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_secs)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await registry.refresh(session)
            if on_refresh is not None:
                on_refresh()
        except Exception as exc:
            logger.warning("scheduled capability refresh failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers used by the handlers
# ---------------------------------------------------------------------------


def normalise_model_for_lookup(model: str) -> str:
    """Strip gateway-side provider prefixes before consulting the registry.

    Mirrors the chat path at ``routing.resolve_route``: callers may pass
    ``openai::gpt-image-1`` (the ``::`` provider-pin form). The registry
    speaks upstream slugs (``gpt-image-1``). The ``provider/model`` form is
    passed through unchanged since it's already an upstream slug — the
    caller (handler) already knows whether the slash is a route marker or
    a real namespace.
    """
    if not model:
        return model
    if "::" in model:
        return model.split("::", 1)[1]
    return model

