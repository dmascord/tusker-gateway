"""Provider adapters for the OpenAI-compatible embeddings endpoint.

Provides a unified ``POST /v1/embeddings`` route backed by a configurable
pool of embedding providers (Synthetic, Voyage, Jina, OpenRouter, etc.).
The handler iterates providers in priority order with fallback, cooldown,
and circuit-breaker tracking — consistent with the rerank pathway.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from tusker_gateway.cooldown import (
    _cooldown_seconds_for_429,
    _cooldown_seconds_for_provider_error,
    global_tracker,
)
from tusker_gateway.errors import (
    BadRequestError,
    GatewayError,
    NoHealthyModelsError,
    ProviderError,
    RateLimitError,
)
from tusker_gateway.passthrough import _persist_cooldown
from tusker_gateway.providers._base_url import (
    _provider_value,
    is_local_provider,
    resolve_base_url,
)

logger = logging.getLogger(__name__)
# Provider priority order — synthetic (free, ZDR) first, then paid, then
# openrouter fallback. ollama-cloud intentionally excluded: ollama.com does
# not expose an OpenAI-compatible /v1/embeddings endpoint, and /api/embed
# rejects the static OLLAMA_API_KEY with 401.
_DEFAULT_PROVIDER_ORDER: tuple[str, ...] = (
    "synthetic",
    "voyage",
    "jina",
    "openrouter",
    "local-llm",
)

_DEFAULT_MODELS: dict[str, str] = {
    "synthetic": "hf:nomic-ai/nomic-embed-text-v1.5",
    "voyage": "voyage-3",
    "jina": "jina-embeddings-v3",
    "openrouter": "sentence-transformers/all-MiniLM-L6-v2",
    "local-llm": "nomic-embed-text",
}

# Provider-specific dimension defaults (used for metadata, not enforcement).
_DEFAULT_DIMENSIONS: dict[str, int] = {
    "synthetic": 768,
    "voyage": 1024,
    "jina": 1024,
    "openrouter": 384,
    "local-llm": 768,
}

# Default base_url overrides applied when the registry entry has none (e.g.
# local Ollama reachable at host.docker.internal rather than localhost).
_EMBED_TIMEOUT_SECS = 30.0

_VIRTUAL_MODELS = frozenset({"hermes-embed", "tusker-gateway/hermes-embed"})


class EmbedProviderUnavailableError(GatewayError):
    """No configured embed provider can accept the request."""

    status = 503
    error_type = "server_error"


@dataclass(frozen=True)
class EmbedBackend:
    provider: str
    url: str
    model: str
    api_key: str
    dimensions: int


@dataclass(frozen=True)
class EmbedRequest:
    input: list[str]
    dimensions: int | None
    encoding_format: str
    budget_units: int




def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


class EmbedHandler:
    """Round-robin embedder with per-provider fallback and cooldowns."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._cursor = 0
        self._lock = threading.Lock()

    @staticmethod
    def _registry(config: dict[str, Any]) -> dict[str, Any]:
        configured = config.get("providers")
        if isinstance(configured, dict) and configured:
            return configured
        from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY

        return DEFAULT_PROVIDER_REGISTRY

    @staticmethod
    def _provider_order() -> tuple[str, ...]:
        raw = (
            os.environ.get("TUSKER_EMBED_PROVIDERS", "").strip()
            or os.environ.get("HERMES_EMBED_PROVIDERS", "").strip()
        )
        if not raw:
            return _DEFAULT_PROVIDER_ORDER
        return tuple(
            item.strip().lower().replace("_", "-")
            for item in raw.split(",")
            if item.strip()
        )

    @staticmethod
    def _default_model(provider: str) -> str:
        suffix = provider.upper().replace("-", "_")
        return (
            os.environ.get(f"TUSKER_EMBED_{suffix}_MODEL", "").strip()
            or os.environ.get(f"HERMES_EMBED_{suffix}_MODEL", "").strip()
            or _DEFAULT_MODELS.get(provider, "")
        )

    @staticmethod
    def _default_dimensions(provider: str) -> int:
        suffix = provider.upper().replace("-", "_")
        try:
            return int(
                os.environ.get(f"TUSKER_EMBED_{suffix}_DIMENSIONS", "").strip()
                or os.environ.get(f"HERMES_EMBED_{suffix}_DIMENSIONS", "").strip()
                or _DEFAULT_DIMENSIONS.get(provider, 768)
            )
        except (TypeError, ValueError):
            return 768

    def _backend_config(self, provider: str) -> Any | None:
        return self._registry(self.config).get(provider)

    def _backend_for(self, provider: str) -> EmbedBackend | None:
        provider_config = self._backend_config(provider.lower().replace("_", "-"))
        if provider_config is None:
            return None

        embed_path = str(
            _provider_value(provider_config, "embed_path", "") or ""
        ).strip()
        base_url = resolve_base_url(
            provider.lower().replace("_", "-"),
            provider_config,
            env_prefix="TUSKER_EMBED",
        ).strip()
        if not embed_path:
            return None
        if not base_url:
            return None
        url = (
            embed_path
            if embed_path.startswith(("http://", "https://"))
            else f"{base_url.rstrip('/')}/{embed_path.lstrip('/')}"
        )

        api_key = str(
            self.config.get("provider_api_keys", {}).get(provider, "") or ""
        ).strip()
        if not api_key and not is_local_provider(provider_config):
            return None
        return EmbedBackend(
            provider=provider,
            url=url,
            model=self._default_model(provider),
            api_key=api_key,
            dimensions=self._default_dimensions(provider),
        )

    def _known_embed_providers(self) -> set[str]:
        return {
            name
            for name in self._provider_order()
            if self._backend_config(name) is not None
        }

    def _resolve_model(self, model: Any) -> tuple[str | None, str | None]:
        """Return ``(provider_pin, model_override)`` for a client model."""
        if model is None or not isinstance(model, str):
            if model is not None:
                raise BadRequestError(
                    "model must be a string", code="invalid_request"
                )
            return None, None

        value = model.strip()
        if not value or value.lower() in _VIRTUAL_MODELS:
            return None, None

        known = self._known_embed_providers()

        if "::" in value:
            provider, _, bare = value.partition("::")
            provider = provider.strip().lower().replace("_", "-")
            if provider not in known:
                raise BadRequestError(
                    f"Embedding provider '{provider}' is not configured",
                    code="unsupported_provider",
                )
            if not bare.strip():
                raise BadRequestError(
                    "model provider pin is missing a model",
                    code="invalid_request",
                )
            return provider, bare.strip()

        if "/" in value:
            provider, _, bare = value.partition("/")
            normalized = provider.strip().lower().replace("_", "-")
            if normalized in known:
                if not bare.strip():
                    raise BadRequestError(
                        "model provider pin is missing a model",
                        code="invalid_request",
                    )
                return normalized, bare.strip()

        # Bare model names: try to infer provider from model prefix.
        lower = value.lower()
        if lower.startswith("hf:") and "synthetic" in known:
            return "synthetic", value
        if lower.startswith("voyage") and "voyage" in known:
            return "voyage", value
        if lower.startswith("jina") and "jina" in known:
            return "jina", value
        if lower.startswith("nomic") and "ollama-cloud" in known:
            return "ollama-cloud", value

        return None, value

    def backends_for_model(
        self, model: Any
    ) -> tuple[list[EmbedBackend], str | None]:
        provider_pin, model_override = self._resolve_model(model)
        if provider_pin:
            backend = self._backend_for(provider_pin)
            if backend is None:
                provider_config = self._backend_config(provider_pin)
                path = (
                    _provider_value(provider_config, "embed_path", "")
                    if provider_config
                    else ""
                )
                if path:
                    raise EmbedProviderUnavailableError(
                        f"Embedding provider '{provider_pin}' has no API key",
                        code="missing_api_key",
                    )
                raise BadRequestError(
                    f"Embedding provider '{provider_pin}' has no embed endpoint",
                    code="unsupported_provider",
                )
            if model_override:
                backend = EmbedBackend(
                    provider=backend.provider,
                    url=backend.url,
                    model=model_override,
                    api_key=backend.api_key,
                    dimensions=backend.dimensions,
                )
            return [backend], model_override

        backends: list[EmbedBackend] = []
        for provider in self._provider_order():
            backend = self._backend_for(provider)
            if backend is None:
                continue
            if model_override:
                backend = EmbedBackend(
                    provider=backend.provider,
                    url=backend.url,
                    model=model_override,
                    api_key=backend.api_key,
                    dimensions=backend.dimensions,
                )
            backends.append(backend)
        return backends, model_override

    @staticmethod
    def validate_request(body: Any) -> EmbedRequest:
        if not isinstance(body, dict):
            raise BadRequestError(
                "Request body must be a JSON object",
                code="invalid_request",
            )

        raw_input = body.get("input")
        if isinstance(raw_input, str):
            if not raw_input.strip():
                raise BadRequestError(
                    "'input' must be a non-empty string or list of strings",
                    code="invalid_request",
                )
            input_list = [raw_input.strip()]
        elif isinstance(raw_input, list):
            if not raw_input:
                raise BadRequestError(
                    "'input' must be a non-empty string or list of strings",
                    code="invalid_request",
                )
            input_list = []
            for item in raw_input:
                if not isinstance(item, str) or not item.strip():
                    raise BadRequestError(
                        "Each item in 'input' must be a non-empty string",
                        code="invalid_request",
                    )
                input_list.append(item.strip())
        else:
            raise BadRequestError(
                "'input' must be a string or list of strings",
                code="invalid_request",
            )

        dimensions = body.get("dimensions")
        if dimensions is not None:
            if not isinstance(dimensions, int) or dimensions < 1:
                raise BadRequestError(
                    "'dimensions' must be a positive integer",
                    code="invalid_request",
                )

        encoding_format = body.get("encoding_format", "float")
        if encoding_format not in ("float", "base64"):
            raise BadRequestError(
                "'encoding_format' must be 'float' or 'base64'",
                code="invalid_request",
            )

        # 1 budget unit per input string.
        budget_units = len(input_list)

        return EmbedRequest(
            input=input_list,
            dimensions=dimensions,
            encoding_format=encoding_format,
            budget_units=budget_units,
        )

    @staticmethod
    def _payload(
        backend: EmbedBackend, request: EmbedRequest
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": backend.model,
            "input": request.input,
        }
        if request.dimensions is not None:
            payload["dimensions"] = request.dimensions
        if request.encoding_format != "float":
            payload["encoding_format"] = request.encoding_format
        return payload

    @staticmethod
    def _error_from_status(
        status: int,
        body: str,
        headers: dict[str, str],
        provider: str,
        model: str,
    ) -> GatewayError:
        if status == 429:
            return RateLimitError(
                "Embedding provider is rate limited; retry shortly",
                code="rate_limit_exceeded",
                body=body,
                headers=headers,
            )
        if status == 401:
            error = ProviderError(
                "Embedding provider authentication failed",
                code="auth_error",
            )
        elif status == 403:
            error = ProviderError(
                "Embedding provider access forbidden",
                code="forbidden",
            )
        elif status >= 500:
            error = ProviderError(
                "Embedding provider returned a server error",
                code="provider_error",
            )
        else:
            error = ProviderError(
                "Embedding provider rejected the request",
                code="provider_error",
            )
        error.upstream_status = status
        error.upstream_body = body
        logger.warning(
            "embed provider rejected request provider=%s model=%s status=%d",
            provider,
            model,
            status,
        )
        return error

    async def _call_backend(
        self,
        backend: EmbedBackend,
        request: EmbedRequest,
        session: aiohttp.ClientSession | Any | None,
    ) -> dict[str, Any]:
        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=_env_float(
                        "TUSKER_EMBED_TIMEOUT_SECS",
                        _EMBED_TIMEOUT_SECS,
                    )
                ),
            )
        try:
            async with session.post(
                backend.url,
                headers={
                    "Authorization": f"Bearer {backend.api_key}",
                    "Content-Type": "application/json",
                },
                json=self._payload(backend, request),
                timeout=aiohttp.ClientTimeout(
                    total=_env_float(
                        "TUSKER_EMBED_TIMEOUT_SECS",
                        _EMBED_TIMEOUT_SECS,
                    )
                ),
            ) as response:
                raw_body = await response.text()
                if not 200 <= response.status < 300:
                    raise self._error_from_status(
                        response.status,
                        raw_body,
                        dict(response.headers),
                        backend.provider,
                        backend.model,
                    )
                try:
                    parsed: dict[str, Any] = __import__("json").loads(raw_body)
                except (ValueError, TypeError) as exc:
                    raise ProviderError(
                        "Embedding provider returned invalid JSON",
                        code="provider_error",
                    ) from exc
                return parsed
        finally:
            if owns_session:
                await session.close()

    @staticmethod
    def _normalize_response(
        raw: dict[str, Any],
        request: EmbedRequest,
        model: str,
    ) -> dict[str, Any]:
        """Normalize provider responses to OpenAI-compatible format.

        Most providers already return the OpenAI shape. This ensures
        consistent structure for callers.
        """
        data = raw.get("data")
        if not isinstance(data, list):
            raise ProviderError(
                "Embedding provider returned no data list",
                code="provider_error",
            )
        if len(data) != len(request.input):
            raise ProviderError(
                f"Embedding provider returned {len(data)} results for "
                f"{len(request.input)} inputs",
                code="provider_error",
            )
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise ProviderError(
                    "Embedding provider returned an invalid result item",
                    code="provider_error",
                )
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise ProviderError(
                    "Embedding provider returned invalid embedding vector",
                    code="provider_error",
                )
            # Ensure index is set correctly.
            if item.get("index") != i:
                item["index"] = i
            # Normalize embedding to float list.
            item["embedding"] = [float(x) for x in embedding]

        output: dict[str, Any] = {
            "object": "list",
            "data": data,
            "model": raw.get("model") or model,
            "usage": raw.get("usage", {"prompt_tokens": 0, "total_tokens": 0}),
        }
        return output

    def _mark_failure(
        self,
        backend: EmbedBackend,
        error: GatewayError,
        breaker: Any | None,
    ) -> None:
        if breaker is not None:
            breaker.record_failure(backend.provider, backend.model)

        if isinstance(error, RateLimitError):
            seconds = _cooldown_seconds_for_429(
                {"body": error.body or "", "headers": error.headers}
            )
        else:
            seconds = _cooldown_seconds_for_provider_error(error)
            if seconds is None and getattr(error, "upstream_status", None) is None:
                try:
                    seconds = max(
                        1.0,
                        float(
                            os.environ.get(
                                "TUSKER_UPSTREAM_FAILURE_COOLDOWN_SECS",
                                "60",
                            )
                        ),
                    )
                except (TypeError, ValueError):
                    seconds = 60.0

        if seconds is not None:
            global_tracker().cooldown(backend.provider, backend.model, seconds)
            _persist_cooldown(
                self.config, backend.provider, backend.model, seconds
            )
        if global_tracker().record_failure(backend.provider):
            provider_seconds = 300.0
            global_tracker().cooldown(backend.provider, "", provider_seconds)
            _persist_cooldown(self.config, backend.provider, "", provider_seconds)

    async def embed(
        self,
        body: dict[str, Any],
        *,
        session: aiohttp.ClientSession | Any | None = None,
        breaker: Any | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        request = self.validate_request(body)
        backends, _ = self.backends_for_model(body.get("model"))
        if not backends:
            raise EmbedProviderUnavailableError(
                "No embedding providers are configured",
                code="no_embed_providers",
            )

        with self._lock:
            start = self._cursor % len(backends)
        ordered = backends[start:] + backends[:start]
        last_error: GatewayError | None = None
        attempted = False

        for position, backend in enumerate(ordered):
            if global_tracker().is_cooldown(backend.provider, backend.model):
                continue
            if breaker is not None and not breaker.check(
                backend.provider,
                backend.model,
            ).allowed:
                continue
            attempted = True

            try:
                raw = await self._call_backend(backend, request, session)
                result = self._normalize_response(raw, request, backend.model)
                if breaker is not None:
                    breaker.record_success(backend.provider, backend.model)
                global_tracker().clear_failures(backend.provider)
                with self._lock:
                    self._cursor = (start + position + 1) % len(backends)
                logger.info(
                    "embed completed provider=%s model=%s inputs=%d",
                    backend.provider,
                    backend.model,
                    len(request.input),
                )
                return backend.provider, backend.model, result
            except GatewayError as exc:
                last_error = exc
                self._mark_failure(backend, exc, breaker)
                logger.warning(
                    "embed backend failed provider=%s model=%s error=%s",
                    backend.provider,
                    backend.model,
                    exc.code or type(exc).__name__,
                )
                continue

        if last_error is not None:
            raise last_error
        if not attempted:
            raise NoHealthyModelsError(pool="embed")
        raise EmbedProviderUnavailableError(
            "All embedding providers are temporarily unavailable; retry shortly",
            code="embed_unavailable",
        )


__all__ = [
    "EmbedHandler",
    "EmbedRequest",
    "EmbedBackend",
    "EmbedProviderUnavailableError",
]
