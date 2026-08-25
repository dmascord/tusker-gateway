"""Text-to-speech dispatch for OpenAI-compatible gateway clients.

OpenAI and OpenRouter accept the public ``/v1/audio/speech`` request shape
directly. Xiaomi MiMo TTS instead uses Chat Completions with an ``audio``
extension; this module translates the request and decodes the returned Base64
audio so callers still receive the OpenAI-compatible binary response.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any, Dict, Optional

import aiohttp

from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
from tusker_gateway.errors import GatewayError

logger = logging.getLogger(__name__)


# Default voice for OpenAI-compatible speech providers.
DEFAULT_VOICE = "alloy"
SUPPORTED_FORMATS = {"mp3", "opus", "aac", "flac", "wav", "pcm"}

XIAOMI_TTS_MODEL_PREFIX = "mimo-v2.5-tts"
XIAOMI_SUPPORTED_FORMATS = {"mp3", "wav", "pcm", "pcm16"}
XIAOMI_CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
    "pcm16": "audio/pcm",
}
MAX_TTS_AUDIO_BYTES = 32 * 1024 * 1024
MAX_XIAOMI_RESPONSE_BYTES = (MAX_TTS_AUDIO_BYTES * 4 // 3) + 1024 * 1024


class TTSHandler:
    """Dispatch text-to-speech requests to a discovered upstream."""

    def __init__(
        self,
        config: Dict[str, Any],
        capability_registry: Optional[Any] = None,
    ):
        self.config = config
        self.capability_registry = capability_registry

    def is_tts_request(self, path: str, body: Dict[str, Any]) -> bool:
        return path == "/v1/audio/speech"

    def get_provider_for_tts_request(
        self,
        model: str,
        capability_registry: Optional[Any] = None,
    ) -> str:
        """Pick the provider, honoring explicit pins before discovery."""
        lower = model.lower()
        if lower.startswith("xiaomi::"):
            return "xiaomi"
        if lower.startswith(("openrouter/", "openrouter::")):
            return "openrouter"
        if "::" in model:
            provider, _, _ = model.partition("::")
            if provider.lower() == "openai":
                return "openai"

        registry = capability_registry or self.capability_registry
        if registry is not None:
            try:
                from tusker_gateway.providers.capabilities import Capability

                entry = registry.snapshot.lookup(Capability.TTS_SPEECH, model)
                if entry is not None:
                    return entry.provider
            except Exception as exc:  # noqa: BLE001
                logger.debug("TTS capability lookup failed, falling back: %s", exc)

        if lower.startswith(XIAOMI_TTS_MODEL_PREFIX):
            return "xiaomi"
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
        if provider == "xiaomi":
            return await self._call_xiaomi(model, body, api_key, extra_headers)
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
    async def _call_xiaomi(
        self,
        model: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> tuple[bytes, str]:
        """Translate Audio Speech to Xiaomi's Chat Completions TTS contract."""
        if not api_key:
            raise GatewayError("Xiaomi API key required for TTS", code="missing_api_key")

        upstream_model = self._strip_xiaomi_prefix(model)
        payload, response_format = self._normalise_xiaomi_request(
            upstream_model, body
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self._xiaomi_tts_url(), headers=headers, json=payload
            ) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:500]
                    logger.warning("Xiaomi TTS failed: %s %s", resp.status, err_text)
                    raise GatewayError(
                        f"Xiaomi TTS error {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > MAX_XIAOMI_RESPONSE_BYTES:
                            raise GatewayError(
                                "Xiaomi TTS response exceeds the gateway size limit",
                                code="upstream_error",
                            )
                    except ValueError:
                        pass
                raw = await resp.read()

        if len(raw) > MAX_XIAOMI_RESPONSE_BYTES:
            raise GatewayError(
                "Xiaomi TTS response exceeds the gateway size limit",
                code="upstream_error",
            )
        try:
            result = json.loads(raw)
            encoded = result["choices"][0]["message"]["audio"]["data"]
            if not isinstance(encoded, str) or not encoded:
                raise KeyError("audio.data")
            if len(encoded) > (MAX_TTS_AUDIO_BYTES * 4 // 3) + 4:
                raise GatewayError(
                    "Xiaomi TTS audio exceeds the gateway size limit",
                    code="upstream_error",
                )
            audio = base64.b64decode(encoded, validate=True)
        except GatewayError:
            raise
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            raise GatewayError(
                "Xiaomi TTS returned an invalid audio response",
                code="upstream_error",
            ) from exc
        if len(audio) > MAX_TTS_AUDIO_BYTES:
            raise GatewayError(
                "Xiaomi TTS audio exceeds the gateway size limit",
                code="upstream_error",
            )
        return audio, XIAOMI_CONTENT_TYPES[response_format]

    def _xiaomi_tts_url(self) -> str:
        configured = self.config.get("providers", {})
        provider = configured.get("xiaomi") if isinstance(configured, dict) else None
        provider = provider or DEFAULT_PROVIDER_REGISTRY["xiaomi"]
        if isinstance(provider, dict):
            base_url = provider.get("base_url")
            chat_path = provider.get("chat_path", "/v1/chat/completions")
        else:
            base_url = provider.base_url
            chat_path = provider.chat_path
        return f"{str(base_url).rstrip('/')}/{str(chat_path).lstrip('/')}"

    @staticmethod
    def _strip_xiaomi_prefix(model: str) -> str:
        if model.lower().startswith("xiaomi::"):
            return model.split("::", 1)[1]
        return model

    @staticmethod
    def _normalise_xiaomi_request(
        model: str,
        body: Dict[str, Any],
    ) -> tuple[Dict[str, Any], str]:
        text = body.get("input")
        if not isinstance(text, str) or not text:
            raise GatewayError("TTS request missing required 'input'", code="bad_request")
        response_format = body.get("response_format") or "mp3"
        if response_format not in XIAOMI_SUPPORTED_FORMATS:
            raise GatewayError(
                f"unsupported Xiaomi response_format '{response_format}'",
                code="bad_request",
            )

        instructions = body.get("instructions")
        messages: list[Dict[str, str]] = []
        audio: Dict[str, Any] = {"format": response_format}
        lower = model.lower()
        if lower.endswith("-voicedesign"):
            if not isinstance(instructions, str) or not instructions.strip():
                raise GatewayError(
                    "Xiaomi voice design requires non-empty 'instructions'",
                    code="bad_request",
                )
            messages.append({"role": "user", "content": instructions})
        else:
            if isinstance(instructions, str) and instructions.strip():
                messages.append({"role": "user", "content": instructions})
            voice = body.get("voice")
            if lower.endswith("-voiceclone"):
                if not isinstance(voice, str) or not voice.startswith(
                    ("data:audio/mpeg;base64,", "data:audio/mp3;base64,", "data:audio/wav;base64,")
                ):
                    raise GatewayError(
                        "Xiaomi voice clone requires an MP3 or WAV data URI in 'voice'",
                        code="bad_request",
                    )
            audio["voice"] = voice or "mimo_default"

        messages.append({"role": "assistant", "content": text})
        return {
            "model": model,
            "messages": messages,
            "audio": audio,
            "stream": False,
        }, response_format

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