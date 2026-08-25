"""Image generation provider support for Tusker AI Gateway.

This module implements image generation support for OpenAI, Google,
OpenRouter, and Z.AI, extending the existing Tusker architecture to handle
image generation requests. Anthropic is intentionally excluded: it supports
image input / text output but no native image output API.
"""

from __future__ import annotations


import json
import logging
import time
from typing import Any, Dict, Optional

import aiohttp

from tusker_gateway.auth_strategies import get_auth_strategy
from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
from tusker_gateway.errors import GatewayError

logger = logging.getLogger(__name__)
MAX_IMAGE_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_RESULTS = 4


async def _read_capped_text(resp: aiohttp.ClientResponse) -> str:
    """Read one image-provider response with a hard memory bound."""
    content_length = getattr(resp, "headers", {}).get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_IMAGE_RESPONSE_BYTES:
                raise GatewayError(
                    "Image provider response exceeds the gateway size limit",
                    code="upstream_error",
                )
        except ValueError:
            pass
    data = bytearray()
    content = getattr(resp, "content", None)
    if content is None:
        data = await resp.read() if hasattr(resp, "read") else (await resp.text()).encode()
        if len(data) > MAX_IMAGE_RESPONSE_BYTES:
            raise GatewayError(
                "Image provider response exceeds the gateway size limit",
                code="upstream_error",
            )
        return data.decode("utf-8")
    data = bytearray()
    async for chunk in content.iter_chunked(1024 * 1024):
        if len(data) + len(chunk) > MAX_IMAGE_RESPONSE_BYTES:
            raise GatewayError(
                "Image provider response exceeds the gateway size limit",
                code="upstream_error",
            )
        data.extend(chunk)
    return data.decode("utf-8")


def _append_image_result(images: list[str], value: Any) -> None:
    if not isinstance(value, str) or not value:
        return
    if len(value.encode("utf-8")) > MAX_IMAGE_RESPONSE_BYTES:
        raise GatewayError(
            "Generated image exceeds the gateway size limit",
            code="upstream_error",
        )
    if value in images:
        return
    if len(images) >= MAX_IMAGE_RESULTS:
        raise GatewayError(
            "Image provider returned too many images",
            code="upstream_error",
        )
    images.append(value)
IMAGE_GEN_MODELS = {
    "gpt-image-2": {"cost_per_1k_tokens": 0.005, "context_window": 8000},
    "gpt-image-1": {"cost_per_1k_tokens": 0.02, "context_window": 8000},
    "gpt-image-1-mini": {"cost_per_1k_tokens": 0.005, "context_window": 8000},
    "dall-e-3": {"cost_per_1k_tokens": 0.04, "context_window": 4096},
    "dall-e-2": {"cost_per_1k_tokens": 0.02, "context_window": 4096},
    # Z.ai PaaS image models. Cost values are per-image (not per-token)
    # so we don't expose them through the cost-per-1k-tokens budget.
    # Listed for catalog discovery only.
    "cogview-4-250304": {"cost_per_image": 0.0, "context_window": 1024},
    "glm-image": {"cost_per_image": 0.0, "context_window": 1024},
    "image-01": {"cost_per_image": 0.0, "context_window": 1500},
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

# Canonical Z.AI model slugs. Z.AI's PaaS API uses mixed case in the
# docs (``cogView-4-250304``) but most clients pass lowercased slugs.
# Dispatch always rewrites to the canonical form before posting.
_ZAI_MODEL_CANONICAL = {
    "cogview-4-250304": "cogView-4-250304",
    "cogview": "cogView-4-250304",
    "cogview4": "cogView-4-250304",
}



class ImageGenerationHandler:
    """Handler for image generation requests in the Tusker gateway."""

    def __init__(
        self,
        config: Dict[str, Any],
        capability_registry: Optional[Any] = None,
    ):
        self.config = config
        self.capability_registry = capability_registry
        configured = config.get("providers", {})
        configured_names = configured.keys() if isinstance(configured, dict) else ()
        self._known_gateway_providers = frozenset(DEFAULT_PROVIDER_REGISTRY).union(
            configured_names,
            {"anthropic", "codex"},
        )

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
            for tag in ("gpt-image", "dall-e", "imagen", "stable-image", "cogview-", "glm-image")
        ):
            return path.startswith("/v1/images/")
        return False

    def get_provider_for_image_request(
        self,
        model: str,
        path: str,
        capability_registry: Optional[Any] = None,
    ) -> str:
        """Determine which provider can serve an image generation request.

        Explicit gateway pins are honoured, while arbitrary upstream namespace
        prefixes remain part of the model id for capability-registry lookup.
        """
        model_lower = model.lower()
        if (
            "claude" in model_lower
            or model_lower.startswith(("anthropic::", "anthropic/"))
            or "/anthropic/" in model_lower
        ):
            raise GatewayError(
                "Anthropic models do not support image generation",
                code="unsupported_model",
            )

        registry = (
            capability_registry
            if capability_registry is not None
            else self.capability_registry
        )

        pin_provider: Optional[str] = None
        if "::" in model:
            candidate, _, _ = model.partition("::")
            candidate = candidate.lower()
            if candidate in self._known_gateway_providers:
                pin_provider = candidate
        elif model_lower.startswith("openrouter/"):
            pin_provider = "openrouter"

        if pin_provider is None and registry is not None:
            try:
                snap = registry.snapshot
                if any(snap.capabilities.values()):
                    from tusker_gateway.providers.capabilities import Capability

                    capability = {
                        "/v1/images/edits": Capability.IMAGE_EDITS,
                        "/v1/images/variations": Capability.IMAGE_VARIATIONS,
                    }.get(path, Capability.IMAGE_GENERATIONS)
                    entry = snap.lookup(capability, model)
                    if entry is not None:
                        if entry.provider == "anthropic":
                            raise GatewayError(
                                "Anthropic models do not support image generation",
                                code="unsupported_model",
                            )
                        return entry.provider
            except GatewayError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("capability registry lookup failed, falling back: %s", exc)

        if pin_provider is None and "/" in model:
            candidate, _, _ = model.partition("/")
            candidate = candidate.lower()
            if candidate in self._known_gateway_providers:
                pin_provider = candidate

        if pin_provider in {"anthropic"}:
            raise GatewayError(
                "Anthropic models do not support image generation",
                code="unsupported_model",
            )
        if pin_provider in {"codex", "openai-codex"}:
            return "openai"
        if pin_provider in {"openai", "openrouter", "google", "zai", "minimax"}:
            return pin_provider
        if pin_provider is not None:
            raise GatewayError(
                f"Provider {pin_provider} does not support image generation",
                code="unsupported_model",
            )

        if "gpt-image" in model_lower or "dall-e" in model_lower:
            return "openai"
        if "gemini" in model_lower or "google" in model_lower or "imagen" in model_lower:
            return "google"
        if "claude" in model_lower or "anthropic" in model_lower:
            raise GatewayError(
                "Anthropic models do not support image generation",
                code="unsupported_model",
            )
        if any(tag in model_lower for tag in ("cogview-", "glm-image")):
            return "zai"
        if model_lower == "image-01":
            return "minimax"
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
        provider = self.get_provider_for_image_request(
            model,
            path,
            capability_registry=self.capability_registry,
        )
        if path == "/v1/images/generations":
            prompt = body.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise GatewayError(
                    "Image generation requires a non-empty prompt",
                    code="missing_prompt",
                )
        if provider == "openai":
            return await self._call_openai(model, path, body, api_key, extra_headers, codex_rotator)
        if provider == "google":
            return await self._call_google(model, path, body, api_key, extra_headers)
        if provider == "openrouter":
            return await self._call_openrouter(model, path, body, api_key, extra_headers)
        if provider == "zai":
            return await self._call_zai(model, path, body, api_key, extra_headers)
        if provider == "minimax":
            return await self._call_minimax(model, path, body, api_key, extra_headers)
        raise GatewayError(
            f"Provider {provider} does not support image generation",
            code="unsupported_model",
        )

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
                text = await _read_capped_text(resp)
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
        # Codex/ChatGPT backend uses model "gpt-5.5" as the Responses engine,
        # and the actual image model slug (e.g. "gpt-image-2") is carried inside
        # the hosted image_generation tool, NOT in the request model field.
        # Sending gpt-image-* as the request model returns "model not supported
        # when using Codex with a ChatGPT account".
        codex_model = "gpt-5.5"
        prompt = body.get("prompt", "")
        size = body.get("size", "1024x1024")
        # Translate OpenAI image request -> Responses API image_generation tool call.
        # The tool produces base64 PNG output via image_generation_call items.
        # The image_generation tool doesn't accept n; for n>1 callers get a single
        # image, which matches the de-facto Codex surface.
        image_model = _map_model_to_codex(model)  # for the tool's model field
        payload: Dict[str, Any] = {
            "model": codex_model,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            "tools": [{"type": "image_generation", "model": image_model, "size": size}],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
            "store": False,
            "reasoning": {"effort": "low", "summary": "auto"},
        }
        url = "https://chatgpt.com/backend-api/codex/responses"
        timeout = aiohttp.ClientTimeout(total=300)
        b64_images: list[str] = []
        retained_bytes = 0
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
                    if len(buffer) + len(chunk) > MAX_IMAGE_RESPONSE_BYTES:
                        raise GatewayError(
                            "Codex image stream frame exceeds the gateway size limit",
                            code="upstream_error",
                        )
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
                                # partial_image events carry intermediate previews in
                                # partial_image_b64; some Codex surfaces also emit a final
                                # result here on the last partial or a completed variant.
                                payload_obj = event.get("image_generation_call") or event.get("item") or event
                                result = payload_obj.get("result") if isinstance(payload_obj, dict) else None
                                if isinstance(result, str) and result:
                                    retained_bytes += len(result.encode("utf-8"))
                                    if retained_bytes > MAX_IMAGE_RESPONSE_BYTES:
                                        raise GatewayError(
                                            "Codex image output exceeds the gateway size limit",
                                            code="upstream_error",
                                        )
                                    _append_image_result(b64_images, result)
                                partial = payload_obj.get("partial_image_b64") if isinstance(payload_obj, dict) else None
                                if isinstance(partial, str) and partial and not b64_images:
                                    retained_bytes += len(partial.encode("utf-8"))
                                    if retained_bytes > MAX_IMAGE_RESPONSE_BYTES:
                                        raise GatewayError(
                                            "Codex image output exceeds the gateway size limit",
                                            code="upstream_error",
                                        )
                                    _append_image_result(b64_images, partial)
                                revised = payload_obj.get("revised_prompt") if isinstance(payload_obj, dict) else None
                                if revised and not revised_prompt:
                                    revised_prompt = revised
                            elif item_type == "response.output_item.done":
                                item = event.get("item") or {}
                                if item.get("type") == "image_generation_call":
                                    result = item.get("result")
                                    if isinstance(result, str) and result:
                                        retained_bytes += len(result.encode("utf-8"))
                                        if retained_bytes > MAX_IMAGE_RESPONSE_BYTES:
                                            raise GatewayError(
                                                "Codex image output exceeds the gateway size limit",
                                                code="upstream_error",
                                            )
                                        _append_image_result(b64_images, result)
                                    rp = item.get("revised_prompt")
                                    if rp and not revised_prompt:
                                        revised_prompt = rp
                            elif item_type == "response.completed":
                                response_obj = event.get("response") or {}
                                for out_item in response_obj.get("output", []):
                                    if out_item.get("type") == "image_generation_call":
                                        result = out_item.get("result")
                                        if isinstance(result, str) and result:
                                            retained_bytes += len(result.encode("utf-8"))
                                            if retained_bytes > MAX_IMAGE_RESPONSE_BYTES:
                                                raise GatewayError(
                                                    "Codex image output exceeds the gateway size limit",
                                                    code="upstream_error",
                                                )
                                            _append_image_result(b64_images, result)
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
        """Call OpenRouter's native image generation endpoint."""
        if not api_key:
            raise GatewayError("OpenRouter API key required", code="missing_api_key")
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise GatewayError(
                "Image generation requires a non-empty prompt",
                code="missing_prompt",
            )
        url = "https://openrouter.ai/api/v1/images"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        payload = {**body, "model": self._strip_provider_prefix(model)}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                text = await _read_capped_text(resp)
                if resp.status >= 400:
                    logger.warning(
                        "OpenRouter image gen failed: %s %s", resp.status, text[:200]
                    )
                    raise GatewayError(
                        f"OpenRouter error {resp.status}: {text[:200]}",
                        code="upstream_error",
                    )
                if not text:
                    raise GatewayError(
                        "OpenRouter image generation returned an empty response",
                        code="upstream_error",
                    )
                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise GatewayError(
                        "OpenRouter image generation returned invalid JSON",
                        code="upstream_error",
                    ) from exc

    @staticmethod
    def _strip_provider_prefix(model: str) -> str:
        """Strip only an explicit OpenRouter gateway pin.

        Upstream namespaces such as ``google/gemini-*`` are part of the model
        slug and must be preserved verbatim.
        """
        model_lower = model.lower()
        if model_lower.startswith("openrouter/"):
            return model[len("openrouter/") :]
        if model_lower.startswith("openrouter::"):
            return model[len("openrouter::") :]
        return model

    @staticmethod
    def _strip_google_prefix(model: str) -> str:
        """Strip only an explicit Google gateway pin."""
        model_lower = model.lower()
        if model_lower.startswith("google/"):
            return model[len("google/") :]
        if model_lower.startswith("google::"):
            return model[len("google::") :]
        return model

    @staticmethod
    def _google_response_format(body: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Map safely representable OpenAI image options to Google config."""
        supplied = body.get("response_format")
        supplied_format = supplied if isinstance(supplied, dict) else {}
        response_format: Dict[str, str] = {"type": "image"}

        aspect_ratio = supplied_format.get("aspect_ratio") or body.get("aspect_ratio")
        valid_aspect_ratios = {"1:1", "16:9", "9:16", "4:3"}
        if aspect_ratio in valid_aspect_ratios:
            response_format["aspect_ratio"] = aspect_ratio

        image_size = supplied_format.get("image_size") or body.get("image_size")
        if isinstance(image_size, str) and image_size.upper() in {"1K", "2K", "4K"}:
            response_format["image_size"] = image_size.upper()

        size = body.get("size")
        if isinstance(size, str) and "x" in size.lower():
            width_text, _, height_text = size.lower().partition("x")
            try:
                width = int(width_text)
                height = int(height_text)
            except ValueError:
                width = height = 0
            for numerator, denominator, label in (
                (1, 1, "1:1"),
                (16, 9, "16:9"),
                (9, 16, "9:16"),
                (4, 3, "4:3"),
            ):
                if width > 0 and width * denominator == height * numerator:
                    response_format.setdefault("aspect_ratio", label)
                    break
            resolution = {1024: "1K", 2048: "2K", 4096: "4K"}.get(
                max(width, height)
            )
            if resolution:
                response_format.setdefault("image_size", resolution)
        elif isinstance(size, str) and size.upper() in {"1K", "2K", "4K"}:
            response_format.setdefault("image_size", size.upper())

        output_format = (
            supplied_format.get("mime_type")
            or supplied_format.get("output_format")
            or (supplied if isinstance(supplied, str) else None)
            or body.get("output_format")
        )
        mime_types = {
            "png": "image/png",
            "image/png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "image/jpeg": "image/jpeg",
            "webp": "image/webp",
            "image/webp": "image/webp",
        }
        if isinstance(output_format, str):
            mime_type = mime_types.get(output_format.lower())
            if mime_type:
                response_format["mime_type"] = mime_type

        return response_format if len(response_format) > 1 else None

    async def _call_google(
        self,
        model: str,
        path: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Call Google's Interactions API and return OpenAI image data."""
        if path in {"/v1/images/edits", "/v1/images/variations"}:
            raise GatewayError(
                "Google image edits and variations are not supported",
                code="unsupported_endpoint",
            )
        if not api_key:
            raise GatewayError("Google API key required", code="missing_api_key")
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise GatewayError(
                "Image generation requires a non-empty prompt",
                code="missing_prompt",
            )

        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        if extra_headers:
            headers.update(extra_headers)
        payload: Dict[str, Any] = {
            "model": self._strip_google_prefix(model),
            "input": [{"type": "text", "text": prompt}],
            "store": False,
        }
        response_format = self._google_response_format(body)
        if response_format:
            payload["generation_config"] = {"response_format": response_format}

        url = "https://generativelanguage.googleapis.com/v1beta/interactions"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                text = await _read_capped_text(resp)
                if resp.status >= 400:
                    logger.warning("Google image gen failed: %s %s", resp.status, text[:200])
                    raise GatewayError(
                        f"Google error {resp.status}: {text[:200]}",
                        code="upstream_error",
                    )
                if not text:
                    raise GatewayError(
                        "Google image generation returned an empty response",
                        code="upstream_error",
                    )
                try:
                    response = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise GatewayError(
                        "Google image generation returned invalid JSON",
                        code="upstream_error",
                    ) from exc

        images = []
        steps = response.get("steps", []) if isinstance(response, dict) else []
        if not isinstance(steps, list):
            steps = []
        for step in steps:
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            contents = step.get("content", [])
            if not isinstance(contents, list):
                continue
            for content in contents:
                if not isinstance(content, dict) or content.get("type") != "image":
                    continue
                data = content.get("data")
                if not isinstance(data, str) or not data:
                    continue
                image = {"b64_json": data}
                media_type = content.get("mime_type")
                if isinstance(media_type, str) and media_type:
                    image["media_type"] = media_type
                images.append(image)
        if not images:
            raise GatewayError(
                "Google image generation returned no image content",
                code="upstream_error",
            )
        return {"created": int(time.time()), "data": images}


    async def _call_zai(
        self,
        model: str,
        path: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Call Z.AI image generation API for CogView-4 and GLM-Image models.
        """
        if not api_key:
            raise GatewayError("Z.AI API key required", code="missing_api_key")

        url = "https://api.z.ai/api/paas/v4/images/generations"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        payload: Dict[str, Any] = {
            "model": _ZAI_MODEL_CANONICAL.get(model.lower(), model),
            "prompt": body.get("prompt", ""),
            "quality": body.get("quality", "hd"),
            "size": body.get("size", "1280x1280"),
        }
        if body.get("user_id"):
            payload["user_id"] = body["user_id"]

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                text = await _read_capped_text(resp)
                if resp.status >= 400:
                    logger.warning(
                        "Z.AI image gen failed: %s %s", resp.status, text[:200]
                    )
                    raise GatewayError(
                        f"Z.AI error {resp.status}: {text[:200]}",
                        code="upstream_error",
                    )
                if not text:
                    return {"status": "ok"}
                return json.loads(text)


    @staticmethod
    def _strip_minimax_prefix(model: str) -> str:
        """Strip an explicit MiniMax gateway pin from the upstream model id."""
        if model.lower().startswith("minimax::"):
            return model[len("minimax::") :]
        if model.lower().startswith("minimax/"):
            return model[len("minimax/") :]
        return model

    async def _call_minimax(
        self,
        model: str,
        path: str,
        body: Dict[str, Any],
        api_key: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Call MiniMax image-01 and normalize its result as OpenAI image data."""
        if path in {"/v1/images/edits", "/v1/images/variations"}:
            raise GatewayError(
                "MiniMax image edits and variations are not supported",
                code="unsupported_endpoint",
            )
        if not api_key:
            raise GatewayError("MiniMax API key required", code="missing_api_key")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        payload: Dict[str, Any] = {
            "model": self._strip_minimax_prefix(model),
            "prompt": body["prompt"],
        }
        aspect_ratio = body.get("aspect_ratio")
        if aspect_ratio in {
            "1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"
        }:
            payload["aspect_ratio"] = aspect_ratio
        width = body.get("width")
        height = body.get("height")
        if (
            isinstance(width, int)
            and not isinstance(width, bool)
            and isinstance(height, int)
            and not isinstance(height, bool)
        ):
            payload["width"] = width
            payload["height"] = height
        response_format = body.get("response_format")
        if response_format in {"url", "base64"}:
            payload["response_format"] = response_format
        seed = body.get("seed")
        if isinstance(seed, int) and not isinstance(seed, bool):
            payload["seed"] = seed
        n = body.get("n")
        if isinstance(n, int) and not isinstance(n, bool):
            payload["n"] = n
        prompt_optimizer = body.get("prompt_optimizer")
        if isinstance(prompt_optimizer, bool):
            payload["prompt_optimizer"] = prompt_optimizer

        url = "https://api.minimax.io/v1/image_generation"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                text = await _read_capped_text(resp)
                if resp.status >= 400:
                    logger.warning(
                        "MiniMax image generation failed: %s %s", resp.status, text[:200]
                    )
                    raise GatewayError(
                        f"MiniMax error {resp.status}: {text[:200]}",
                        code="upstream_error",
                    )
                if not text:
                    raise GatewayError(
                        "MiniMax image generation returned an empty response",
                        code="upstream_error",
                    )
                try:
                    response = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise GatewayError(
                        "MiniMax image generation returned invalid JSON",
                        code="upstream_error",
                    ) from exc

        if not isinstance(response, dict):
            raise GatewayError(
                "MiniMax image generation returned an invalid response",
                code="upstream_error",
            )
        base_resp = response.get("base_resp")
        if isinstance(base_resp, dict) and base_resp.get("status_code") not in (None, 0):
            status_code = base_resp.get("status_code")
            status_msg = base_resp.get("status_msg", "unknown error")
            raise GatewayError(
                f"MiniMax error {status_code}: {status_msg}",
                code="upstream_error",
            )

        upstream_data = response.get("data")
        if not isinstance(upstream_data, dict):
            upstream_data = {}
        images: list[Dict[str, str]] = []
        image_urls = upstream_data.get("image_urls")
        if isinstance(image_urls, list):
            for value in image_urls:
                if isinstance(value, str) and value:
                    if len(value.encode("utf-8")) > MAX_IMAGE_RESPONSE_BYTES:
                        raise GatewayError(
                            "MiniMax image generation returned an oversized image result",
                            code="upstream_error",
                        )
                    if len(images) >= MAX_IMAGE_RESULTS:
                        raise GatewayError(
                            "MiniMax image generation returned too many images",
                            code="upstream_error",
                        )
                    images.append({"url": value})
        image_base64 = upstream_data.get("image_base64")
        if isinstance(image_base64, list):
            for value in image_base64:
                if isinstance(value, str) and value:
                    if len(value.encode("utf-8")) > MAX_IMAGE_RESPONSE_BYTES:
                        raise GatewayError(
                            "MiniMax image generation returned an oversized image result",
                            code="upstream_error",
                        )
                    if len(images) >= MAX_IMAGE_RESULTS:
                        raise GatewayError(
                            "MiniMax image generation returned too many images",
                            code="upstream_error",
                        )
                    images.append({"b64_json": value})
        if not images:
            raise GatewayError(
                "MiniMax image generation returned no images",
                code="upstream_error",
            )
        return {"created": int(time.time()), "data": images}


def get_image_generation_handler(
    config: Dict[str, Any],
    capability_registry: Optional[Any] = None,
) -> ImageGenerationHandler:
    """Get or create an image generation handler instance."""
    return ImageGenerationHandler(config, capability_registry=capability_registry)