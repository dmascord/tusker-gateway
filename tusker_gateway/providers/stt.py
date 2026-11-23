"""Speech-to-text dispatch for OpenAI-compatible gateway clients.

``POST /v1/audio/transcriptions`` is an OpenAI-shaped multipart endpoint. The
gateway accepts the standard fields (``file``, ``model``, and the optional
``language`` / ``prompt`` / ``response_format`` / ``temperature``) and forwards
them to an OpenAI-compatible upstream, returning the upstream JSON verbatim.
Provider selection mirrors :mod:`tusker_gateway.providers.tts`: an explicit
``provider::model`` (or ``provider/model``) pin wins, then the capability
registry, then a conservative OpenAI default.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import aiohttp

from tusker_gateway.errors import GatewayError

logger = logging.getLogger(__name__)

# OpenAI's documented upload limit for audio transcriptions.
MAX_STT_AUDIO_BYTES = 25 * 1024 * 1024
ALLOWED_FORM_FIELDS = ("language", "prompt", "response_format", "temperature")
PROVIDER_URLS = {
    "openai": "https://api.openai.com/v1/audio/transcriptions",
    "groq": "https://api.groq.com/openai/v1/audio/transcriptions",
    "openrouter": "https://openrouter.ai/api/v1/audio/transcriptions",
}
# Prefixes accepted in a client-supplied model slug.
_PINNED_PREFIXES = ("openai", "groq", "openrouter")


class STTHandler:
    """Dispatch transcription requests to an OpenAI-compatible upstream."""

    MAX_STT_AUDIO_BYTES = MAX_STT_AUDIO_BYTES

    def __init__(self, config: Dict[str, Any], capability_registry: Any | None = None):
        self.config = config
        self.capability_registry = capability_registry

    def get_provider_for_stt_request(
        self,
        model: str,
        capability_registry: Any | None = None,
    ) -> str:
        """Pick the upstream provider for *model*."""
        lower = str(model or "").strip().lower()
        for provider in _PINNED_PREFIXES:
            if lower.startswith((provider + "::", provider + "/")):
                return provider
        registry = capability_registry or self.capability_registry
        if registry is not None:
            try:
                from tusker_gateway.providers.capabilities import Capability

                entry = registry.snapshot.lookup(
                    Capability.SPEECH_TRANSCRIPTION, model
                )
                if entry is not None:
                    return entry.provider
            except Exception as exc:  # noqa: BLE001
                logger.debug("STT capability lookup failed, falling back: %s", exc)
        return "openai"

    @staticmethod
    def normalize_model(model: str, provider: str) -> str:
        """Strip a provider pin so the upstream receives a bare model id."""
        value = str(model or "").strip()
        for prefix in _PINNED_PREFIXES:
            for separator in ("::", "/"):
                prefix_text = prefix + separator
                if value.lower().startswith(prefix_text):
                    return value[len(prefix_text):].strip()
        return value

    async def handle_request(
        self,
        *,
        model: str,
        form_fields: Dict[str, Any],
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        api_key: str | None,
    ) -> Dict[str, Any]:
        """POST one transcription and return the upstream JSON response.

        Raises ``GatewayError`` on upstream failure so the endpoint can render a
        sanitized OpenAI-shaped error without echoing provider bodies.
        """
        if not audio_bytes:
            raise GatewayError("empty audio upload", code="invalid_request")
        if len(audio_bytes) > MAX_STT_AUDIO_BYTES:
            raise GatewayError(
                f"audio upload exceeds {MAX_STT_AUDIO_BYTES} bytes",
                code="payload_too_large",
            )
        provider = self.get_provider_for_stt_request(model)
        url = PROVIDER_URLS[provider]
        if not api_key:
            raise GatewayError(
                f"{provider} API key required for transcription",
                code="missing_api_key",
            )
        form = aiohttp.FormData()
        form.add_field(
            "file",
            audio_bytes,
            filename=filename or "audio.wav",
            content_type=content_type or "application/octet-stream",
        )
        form.add_field("model", self.normalize_model(model, provider))
        for key in ALLOWED_FORM_FIELDS:
            if key in form_fields and form_fields[key] is not None:
                form.add_field(key, str(form_fields[key]))
        headers = {"Authorization": f"Bearer {api_key}"}
        timeout = aiohttp.ClientTimeout(total=120, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=form, headers=headers) as resp:
                if resp.status >= 400:
                    body = (await resp.text())[:500]
                    logger.warning(
                        "STT upstream error provider=%s status=%s body=%s",
                        provider, resp.status, body,
                    )
                    raise GatewayError(
                        f"transcription upstream error {resp.status}",
                        code="upstream_error",
                    )
                try:
                    result = await resp.json(content_type=None)
                except Exception as exc:  # noqa: BLE001
                    raise GatewayError(
                        "transcription upstream returned invalid JSON",
                        code="upstream_error",
                    ) from exc
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise GatewayError(
                "transcription upstream response missing text",
                code="upstream_error",
            )
        return result


def get_stt_handler(config: Dict[str, Any]) -> STTHandler:
    """Build an STT handler from the gateway config."""
    return STTHandler(config)
