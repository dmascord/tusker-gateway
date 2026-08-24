"""Image generation provider support for Tusker AI Gateway.

This module implements image generation support for OpenAI, Google, OpenRouter,
and Anthropic, extending the existing Tusker architecture to handle image
generation requests.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

import aiohttp

from tusker_gateway.copilot_constants import is_likely_vision_model
from tusker_gateway.errors import GatewayError

logger = logging.getLogger(__name__)

IMAGE_GEN_MODELS = {
    "gpt-image-2": {"cost_per_1k_tokens": 0.005, "context_window": 8000},
    "gpt-image-1": {"cost_per_1k_tokens": 0.02, "context_window": 8000},
    "gpt-image-1-mini": {"cost_per_1k_tokens": 0.005, "context_window": 8000},
    "dall-e-3": {"cost_per_1k_tokens": 0.04, "context_window": 4096},
    "dall-e-2": {"cost_per_1k_tokens": 0.02, "context_window": 4096},
}


class ImageGenerationHandler:
    """Handler for image generation requests in the Tusker gateway."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def is_image_generation_request(
        self,
        method: str,
        path: str,
        model: Optional[str] = None,
    ) -> bool:
        """Check if this is an image generation request."""
        if method.upper() != "POST":
            return False
        if path in (
            "/v1/images/generations",
            "/v1/images/edits",
            "/v1/images/variations",
        ):
            return True
        if model and any(
            tag in model.lower()
            for tag in ("gpt-image", "dall-e", "imagen", "stable-image")
        ):
            return path.startswith("/v1/images/")
        return False

    def get_provider_for_image_request(
        self,
        model: str,
        path: str,
    ) -> str:
        """Determine which provider to use for an image generation request."""
        model_lower = model.lower()
        if "/" in model:
            return "openrouter"
        if "gpt-image" in model_lower or "dall-e" in model_lower:
            return "openai"
        if "gemini" in model_lower or "google" in model_lower or "imagen" in model_lower:
            return "google"
        if "claude" in model_lower or "anthropic" in model_lower:
            return "anthropic"
        return "openai"

    async def handle_request(
        self,
        model: str,
        path: str,
        body: Dict[str, Any],
        api_key: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Handle an image generation request by dispatching to the right provider."""
        provider = self.get_provider_for_image_request(model, path)
        if provider == "openai":
            return await self._call_openai(model, path, body, api_key, extra_headers)
        if provider == "google":
            return await self._call_google(model, path, body, api_key, extra_headers)
        if provider == "openrouter":
            return await self._call_openrouter(model, path, body, api_key, extra_headers)
        if provider == "anthropic":
            return await self._call_anthropic(model, path, body, api_key, extra_headers)
        return {
            "created": int(time.time()),
            "provider": provider,
            "model": model,
            "path": path,
            "status": "provider_not_configured",
            "message": f"Image generation via {provider} not configured",
        }

    async def _call_openai(
        self,
        model: str,
        path: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Call OpenAI image generation API."""
        if not api_key:
            raise GatewayError("OpenAI API key required", code="missing_api_key")
        url = "https://api.openai.com" + path
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logger.warning("OpenAI image gen failed: %s %s", resp.status, text[:200])
                    raise GatewayError(
                        f"OpenAI error {resp.status}: {text[:200]}",
                        code="upstream_error",
                    )
                if not text:
                    return {"status": "ok"}
                return json.loads(text)

    async def _call_openrouter(
        self,
        model: str,
        path: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Call OpenRouter for vision/image models exposed under provider/model."""
        if not api_key:
            raise GatewayError("OpenRouter API key required", code="missing_api_key")
        url = "https://openrouter.ai/api/v1/images/generations"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        payload = {**body, "model": model}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logger.warning(
                        "OpenRouter image gen failed: %s %s", resp.status, text[:200]
                    )
                    raise GatewayError(
                        f"OpenRouter error {resp.status}: {text[:200]}",
                        code="upstream_error",
                    )
                if not text:
                    return {"status": "ok"}
                return json.loads(text)

    async def _call_google(
        self,
        model: str,
        path: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Call Google image generation API (Imagen or Gemini)."""
        if not api_key:
            raise GatewayError("Google API key required", code="missing_api_key")
        headers = {"Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        if model.lower().startswith("imagen"):
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
                f":predict?key={api_key}"
            )
            payload = {
                "instances": [{"prompt": body.get("prompt", "")}],
                "parameters": {"sampleCount": body.get("n", 1)},
            }
        else:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
                f":generateContent?key={api_key}"
            )
            payload = {"contents": [{"parts": [{"text": body.get("prompt", "")}]}]}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logger.warning("Google image gen failed: %s %s", resp.status, text[:200])
                    raise GatewayError(
                        f"Google error {resp.status}: {text[:200]}",
                        code="upstream_error",
                    )
                if not text:
                    return {"status": "ok"}
                return json.loads(text)

    async def _call_anthropic(
        self,
        model: str,
        path: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Call Anthropic API for image understanding (no native image gen)."""
        if not api_key:
            raise GatewayError("Anthropic API key required", code="missing_api_key")
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        payload = {
            "model": model,
            "max_tokens": body.get("max_tokens", 1024),
            "messages": [{"role": "user", "content": body.get("prompt", "")}],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logger.warning("Anthropic image gen failed: %s %s", resp.status, text[:200])
                    raise GatewayError(
                        f"Anthropic error {resp.status}: {text[:200]}",
                        code="upstream_error",
                    )
                if not text:
                    return {"status": "ok"}
                return json.loads(text)


def get_image_generation_handler(config: Dict[str, Any]) -> ImageGenerationHandler:
    """Get or create an image generation handler instance."""
    return ImageGenerationHandler(config)