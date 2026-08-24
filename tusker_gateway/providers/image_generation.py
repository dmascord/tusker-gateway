"""Image generation provider support for Tusker AI Gateway.

This module implements image generation support for OpenAI, Google, OpenRouter,
and Anthropic, extending the existing Tusker architecture to handle image
generation requests.
"""

from __future__ import annotations


import base64
import json
import logging
import time
from typing import Any, Dict, Optional

import aiohttp

from tusker_gateway.auth_strategies import get_auth_strategy
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

# Map common OpenAI image model names to the slugs the Codex backend accepts.
def _map_model_to_codex(model: str) -> str:
    """Translate an OpenAI image model id to the Codex image generation model slug.

    Codex/ChatGPT currently only serves image generation through ``gpt-image-2``
    when authenticated with a ChatGPT account; ``gpt-image-1`` returns
    "model not supported when using Codex with a ChatGPT account".
    Older DALL-E names also route to ``gpt-image-2`` since that's the
    available slug.
    """
    lower = (model or "").lower()
    if "gpt-image" in lower or "dall-e" in lower or lower in {"", "auto"}:
        return "gpt-image-2"
    return model


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
        codex_rotator: Any = None,
    ) -> Dict[str, Any]:
        """Handle an image generation request by dispatching to the right provider."""
        provider = self.get_provider_for_image_request(model, path)
        if provider == "openai":
            return await self._call_openai(model, path, body, api_key, extra_headers, codex_rotator)
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
        codex_rotator: Any = None,
    ) -> Dict[str, Any]:
        """Call OpenAI image generation API; falls back to Codex OAuth pathway."""
        if api_key:
            return await self._call_openai_direct(model, path, body, api_key, extra_headers)
        if codex_rotator:
            return await self._call_openai_codex(model, path, body, codex_rotator, extra_headers)
        raise GatewayError(
            "OpenAI image generation requires either OPENAI_API_KEY or Codex OAuth credentials",
            code="missing_api_key",
        )

    async def _call_openai_direct(
        self,
        model: str,
        path: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Call api.openai.com /v1/images/* directly with an OpenAI API key."""
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

    async def _call_openai_codex(
        self,
        model: str,
        path: str,
        body: Dict[str, Any],
        codex_rotator: Any,
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Generate images via Codex OAuth (chatgpt.com/backend-api/codex/responses).

        Used when no OPENAI_API_KEY is available but Codex OAuth credentials are.
        Sends a Responses API request with the native ``image_generation`` tool,
        which is what Codex / ChatGPT currently exposes for GPT Image models.
        """
        from tusker_gateway.models import ProviderConfig
        endpoint = ProviderConfig.from_raw({
            "base_url": "https://chatgpt.com/backend-api/codex",
            "chat_path": "/responses",
            "auth_type": "codex",
            "model_header": "x-openai-gpt-model",
        })
        strategy = get_auth_strategy("codex", codex_rotator)
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **(extra_headers or {}),
            **await strategy.headers(self.config, "openai-codex", model, None, endpoint),
        }
        # Codex backend currently accepts the model slug directly (e.g.
        # "gpt-image-1"); the Responses API with image_generation tool handles
        # the rest.
        codex_model = _map_model_to_codex(model)
        prompt = body.get("prompt", "")
        size = body.get("size", "1024x1024")
        n = int(body.get("n", 1))
        # Translate OpenAI image request -> Responses API image_generation tool call.
        # The tool produces base64 PNG output via image_generation_call items.
        payload: Dict[str, Any] = {
            "model": codex_model,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            "tools": [{"type": "image_generation", "size": size, "n": n}],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
            "store": False,
            "reasoning": {"effort": "low", "summary": "auto"},
        }
        url = "https://chatgpt.com/backend-api/codex/responses"
        timeout = aiohttp.ClientTimeout(total=300)
        b64_images: list[str] = []
        revised_prompt: Optional[str] = None
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    err_text = (await resp.text())[:300]
                    logger.warning("Codex image gen failed: %s %s", resp.status, err_text)
                    raise GatewayError(
                        f"Codex image gen error {resp.status}: {err_text}",
                        code="upstream_error",
                    )
                # Stream SSE and harvest image_generation_call output items.
                buffer = b""
                async for chunk in resp.content.iter_any():
                    buffer += chunk
                    while b"\n\n" in buffer:
                        frame, buffer = buffer.split(b"\n\n", 1)
                        for line in frame.splitlines():
                            line = line.strip()
                            if not line.startswith(b"data: "):
                                continue
                            data = line[len(b"data: "):]
                            if data == b"[DONE]":
                                continue
                            try:
                                event = json.loads(data)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue
                            item_type = event.get("type", "")
                            if ".image_generation_call" in item_type or item_type == "image_generation_call":
                                payload_obj = event.get("image_generation_call") or event.get("item") or event
                                result = payload_obj.get("result") if isinstance(payload_obj, dict) else None
                                if isinstance(result, str) and result:
                                    b64_images.append(result)
                                revised = payload_obj.get("revised_prompt") if isinstance(payload_obj, dict) else None
                                if revised and not revised_prompt:
                                    revised_prompt = revised
                            elif item_type == "response.completed":
                                response_obj = event.get("response") or {}
                                for out_item in response_obj.get("output", []):
                                    if out_item.get("type") == "image_generation_call":
                                        result = out_item.get("result")
                                        if isinstance(result, str) and result:
                                            b64_images.append(result)
                                        rp = out_item.get("revised_prompt")
                                        if rp and not revised_prompt:
                                            revised_prompt = rp
        if not b64_images:
            raise GatewayError(
                "Codex image gen returned no images",
                code="upstream_error",
            )
        data = [
            {"b64_json": b64, "revised_prompt": revised_prompt}
            for b64 in b64_images
        ]
        if len(data) == 1:
            data[0].pop("revised_prompt", None)
        return {
            "created": int(time.time()),
            "data": data,
        }

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