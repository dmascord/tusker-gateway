"""Text-to-speech provider support for Tusker AI Gateway.

Supports:
- OpenAI native TTS (/v1/audio/speech) when OPENAI_API_KEY is set.
- OpenRouter TTS (/api/v1/audio/speech) when model is provider-prefixed
  (e.g. "openai/gpt-4o-mini-tts") or when no OpenAI key is available.

Both APIs share the same request/response shape:
  POST {input, model, voice, response_format, speed} -> binary audio bytes
Response Content-Type is determined by the upstream (audio/mpeg for mp3,
audio/pcm for pcm, audio/opus for opus).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import aiohttp

from tusker_gateway.errors import GatewayError

logger = logging.getLogger(__name__)


# Default voice for providers that require one.
DEFAULT_VOICE = "alloy"

# Map OpenAI image model name conventions → audio response format choices.
SUPPORTED_FORMATS = {"mp3", "opus", "aac", "flac", "wav", "pcm"}


class TTSHandler:
    """Dispatch text-to-speech requests to OpenAI or OpenRouter."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def is_tts_request(self, path: str, body: Dict[str, Any]) -> bool:
        return path == "/v1/audio/speech"

    def get_provider_for_tts_request(self, model: str) -> str:
        """Pick the provider for a TTS request.

        Provider-prefixed slugs (e.g. "openai/gpt-4o-mini-tts") go to
        OpenRouter; bare OpenAI model names (gpt-4o-mini-tts,
        tts-1, tts-1-hd) go to OpenAI when a key is configured.
        """
        if "/" in model:
            return "openrouter"
        return "openai"

    async def handle_request(
        self,
        model: str,
        body: Dict[str, Any],
        api_key: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> tuple[bytes, str]:
        """Run a TTS request and return (audio_bytes, content_type).

        Raises GatewayError on upstream failure.
        """
        provider = self.get_provider_for_tts_request(model)
        if provider == "openrouter":
            return await self._call_openrouter(model, body, api_key, extra_headers)
        return await self._call_openai(model, body, api_key, extra_headers)

    async def _call_openai(
        self,
        model: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> tuple[bytes, str]:
        if not api_key:
            raise GatewayError("OpenAI API key required for TTS", code="missing_api_key")
        url = "https://api.openai.com/v1/audio/speech"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        payload = self._normalise_request(model, body)
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:500]
                    logger.warning("OpenAI TTS failed: %s %s", resp.status, err_text)
                    raise GatewayError(
                        f"OpenAI TTS error {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                audio = await resp.read()
                content_type = resp.headers.get("Content-Type", "audio/mpeg")
                return audio, content_type

    async def _call_openrouter(
        self,
        model: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> tuple[bytes, str]:
        if not api_key:
            raise GatewayError("OpenRouter API key required for TTS", code="missing_api_key")
        url = "https://openrouter.ai/api/v1/audio/speech"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        payload = self._normalise_request(self._strip_provider_prefix(model), body)
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:500]
                    logger.warning("OpenRouter TTS failed: %s %s", resp.status, err_text)
                    raise GatewayError(
                        f"OpenRouter TTS error {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                audio = await resp.read()
                content_type = resp.headers.get("Content-Type", "audio/mpeg")
                return audio, content_type

    @staticmethod
    def _strip_provider_prefix(model: str) -> str:
        """Strip a leading 'openrouter/' prefix before sending upstream.

        The gateway accepts both 'model' and 'openrouter/model' request ids so
        clients can pin the routing provider; OpenRouter itself does not
        understand its own prefix, so it must be removed before the upstream
        call. Mirrors routing.resolve_route's slash-form partition.
        """
        if model.startswith(("openrouter/", "openrouter::")):
            return model.split("/", 1)[1] if "/" in model else model.split("::", 1)[1]
        return model

    @staticmethod
    def _normalise_request(model: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Translate the OpenAI Audio Speech request shape.

        Required: input. Optional: voice (defaults to "alloy"),
        response_format (defaults to "mp3"), speed (defaults to 1.0).
        """
        text = body.get("input", "")
        if not text:
            raise GatewayError("TTS request missing required 'input'", code="bad_request")
        voice = body.get("voice") or DEFAULT_VOICE
        fmt = body.get("response_format") or "mp3"
        if fmt not in SUPPORTED_FORMATS:
            raise GatewayError(
                f"unsupported response_format '{fmt}'",
                code="bad_request",
            )
        speed = body.get("speed", 1.0)
        payload: Dict[str, Any] = {
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": fmt,
        }
        # Only forward speed for OpenAI models; OpenRouter ignores it for
        # providers that don't support it but it's harmless to send.
        if speed is not None:
            try:
                payload["speed"] = float(speed)
            except (TypeError, ValueError):
                raise GatewayError(f"invalid speed value: {speed}", code="bad_request")
        return payload


def get_tts_handler(config: Dict[str, Any]) -> TTSHandler:
    return TTSHandler(config)