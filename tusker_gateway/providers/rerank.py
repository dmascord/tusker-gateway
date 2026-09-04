"""Provider adapters for the Cohere-compatible rerank endpoint.

The legacy Hermes gateway exposed ``POST /v1/rerank`` as a small provider
pool. Keep that public contract here, but use the gateway's normalized
provider registry and shared HTTP session so authentication, cooldowns,
breakers, and access logging remain consistent with the other routes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
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

logger = logging.getLogger(__name__)

_DEFAULT_PROVIDER_ORDER = ("cohere", "voyage", "jina")
_DEFAULT_MODELS = {
    "cohere": "rerank-v3.5",
    "voyage": "rerank-2",
    "jina": "jina-reranker-v2-base-multilingual",
}
_RERANK_TIMEOUT_SECS = 30.0
_DEFAULT_MAX_DOCUMENTS = 1_000
_VIRTUAL_MODELS = frozenset({"hermes-reranker", "tusker-gateway/hermes-reranker"})


class RerankerUnavailableError(GatewayError):
    """No configured reranker backend can accept the request."""

    status = 503
    error_type = "server_error"


@dataclass(frozen=True)
class RerankBackend:
    provider: str
    url: str
    model: str
    api_key: str
    style: str


@dataclass(frozen=True)
class RerankRequest:
    query: str
    documents: tuple[str, ...]
    source_documents: tuple[Any, ...]
    top_n: int | None
    return_documents: bool
    max_tokens_per_doc: int | None
    priority: int | None
    truncation: bool | None
    budget_units: int


def _provider_value(provider_config: Any, field: str, default: Any = None) -> Any:
    if isinstance(provider_config, dict):
        return provider_config.get(field, default)
    return getattr(provider_config, field, default)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequestError(f"{field} must be an integer", code="invalid_request")
    if value < 1:
        raise BadRequestError(f"{field} must be at least 1", code="invalid_request")
    return value


def _optional_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise BadRequestError(f"{field} must be a boolean", code="invalid_request")
    return value


class RerankHandler:
    """Round-robin reranker with per-provider fallback and cooldowns."""

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
            os.environ.get("TUSKER_RERANKER_PROVIDERS", "").strip()
            or os.environ.get("HERMES_RERANKER_PROVIDERS", "").strip()
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
            os.environ.get(f"TUSKER_RERANKER_{suffix}_MODEL", "").strip()
            or os.environ.get(f"HERMES_RERANKER_{suffix}_MODEL", "").strip()
            or _DEFAULT_MODELS.get(provider, "")
        )

    def _backend_config(self, provider: str) -> Any | None:
        return self._registry(self.config).get(provider)

    def _backend_for(self, provider: str) -> RerankBackend | None:
        provider = provider.lower().replace("_", "-")
        provider_config = self._backend_config(provider)
        if provider_config is None:
            return None
        path = str(_provider_value(provider_config, "rerank_path", "") or "").strip()
        base_url = str(_provider_value(provider_config, "base_url", "") or "").strip()
        if not path:
            return None
        url = path if path.startswith(("http://", "https://")) else (
            f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        )
        api_key = str(
            self.config.get("provider_api_keys", {}).get(provider, "") or ""
        ).strip()
        if not api_key:
            return None
        return RerankBackend(
            provider=provider,
            url=url,
            model=self._default_model(provider),
            api_key=api_key,
            style=provider,
        )

    def _known_rerank_providers(self) -> set[str]:
        return {
            name
            for name in self._provider_order()
            if self._backend_config(name) is not None
        }

    def _resolve_model(self, model: Any) -> tuple[str | None, str | None]:
        """Return ``(provider_pin, model_override)`` for a client model."""
        if model is None or not isinstance(model, str):
            if model is not None:
                raise BadRequestError("model must be a string", code="invalid_request")
            return None, None
        value = model.strip()
        if not value or value.lower() in _VIRTUAL_MODELS:
            return None, None

        known = self._known_rerank_providers()
        if "::" in value:
            provider, _, bare = value.partition("::")
            provider = provider.strip().lower().replace("_", "-")
            if provider not in known:
                raise BadRequestError(
                    f"Reranking provider '{provider}' is not configured",
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
            normalized_provider = provider.strip().lower().replace("_", "-")
            if normalized_provider in known:
                if not bare.strip():
                    raise BadRequestError(
                        "model provider pin is missing a model",
                        code="invalid_request",
                    )
                return normalized_provider, bare.strip()

        # Bare model names remain compatible with the old Hermes router. Use
        # obvious model families to avoid sending a Voyage model to Cohere.
        lower = value.lower()
        if lower.startswith("jina-") and "jina" in known:
            return "jina", value
        if lower.startswith(("rerank-2", "rerank-1")) and "voyage" in known:
            return "voyage", value
        if lower.startswith("rerank-") and "cohere" in known:
            return "cohere", value
        return None, value

    def backends_for_model(self, model: Any) -> tuple[list[RerankBackend], str | None]:
        provider_pin, model_override = self._resolve_model(model)
        if provider_pin:
            backend = self._backend_for(provider_pin)
            if backend is None:
                provider_config = self._backend_config(provider_pin)
                path = (
                    _provider_value(provider_config, "rerank_path", "")
                    if provider_config
                    else ""
                )
                if path:
                    raise RerankerUnavailableError(
                        f"Reranking provider '{provider_pin}' has no API key",
                        code="missing_api_key",
                    )
                raise BadRequestError(
                    f"Reranking provider '{provider_pin}' has no rerank endpoint",
                    code="unsupported_provider",
                )
            if model_override:
                backend = RerankBackend(
                    provider=backend.provider,
                    url=backend.url,
                    model=model_override,
                    api_key=backend.api_key,
                    style=backend.style,
                )
            return [backend], model_override

        backends: list[RerankBackend] = []
        for provider in self._provider_order():
            backend = self._backend_for(provider)
            if backend is None:
                continue
            if model_override:
                backend = RerankBackend(
                    provider=backend.provider,
                    url=backend.url,
                    model=model_override,
                    api_key=backend.api_key,
                    style=backend.style,
                )
            backends.append(backend)
        return backends, model_override

    @staticmethod
    def validate_request(body: Any) -> RerankRequest:
        if not isinstance(body, dict):
            raise BadRequestError(
                "Request body must be a JSON object",
                code="invalid_request",
            )

        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            raise BadRequestError(
                "'query' is required and must be a non-empty string",
                code="invalid_request",
            )

        raw_documents = body.get("documents")
        if not isinstance(raw_documents, list) or not raw_documents:
            raise BadRequestError(
                "'documents' is required and must be a non-empty list",
                code="invalid_request",
            )
        max_documents = _env_int(
            "TUSKER_RERANKER_MAX_DOCUMENTS",
            _DEFAULT_MAX_DOCUMENTS,
        )
        if len(raw_documents) > max_documents:
            raise BadRequestError(
                f"'documents' exceeds the gateway limit of {max_documents}",
                code="invalid_request",
            )

        rank_fields = body.get("rank_fields")
        if rank_fields is not None:
            if (
                not isinstance(rank_fields, list)
                or not rank_fields
                or any(
                    not isinstance(field, str) or not field.strip()
                    for field in rank_fields
                )
            ):
                raise BadRequestError(
                    "'rank_fields' must be a non-empty list of strings",
                    code="invalid_request",
                )

        documents: list[str] = []
        for index, document in enumerate(raw_documents):
            if isinstance(document, str):
                text = document
            elif isinstance(document, dict) and isinstance(document.get("text"), str):
                if rank_fields:
                    values = [document.get(field, "") for field in rank_fields]
                    text = "\n".join(
                        str(value) for value in values if value is not None
                    )
                else:
                    text = document["text"]
            else:
                raise BadRequestError(
                    f"documents[{index}] must be a string or an object with a string 'text' field",
                    code="invalid_request",
                )
            if not text.strip():
                raise BadRequestError(
                    f"documents[{index}] must not be empty",
                    code="invalid_request",
                )
            documents.append(text)

        top_n = body.get("top_n")
        top_k = body.get("top_k")
        if top_n is not None and top_k is not None and top_n != top_k:
            raise BadRequestError(
                "top_n and top_k must match when both are provided",
                code="invalid_request",
            )
        top_value = top_n if top_n is not None else top_k
        parsed_top = (
            _positive_int(top_value, "top_n") if top_value is not None else None
        )

        return_documents = _optional_bool(
            body.get("return_documents"),
            "return_documents",
        )
        max_tokens = body.get("max_tokens_per_doc")
        parsed_max_tokens = (
            _positive_int(max_tokens, "max_tokens_per_doc")
            if max_tokens is not None
            else None
        )
        priority = body.get("priority")
        if priority is not None:
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise BadRequestError(
                    "priority must be an integer",
                    code="invalid_request",
                )
            if priority < 0 or priority > 999:
                raise BadRequestError(
                    "priority must be between 0 and 999",
                    code="invalid_request",
                )
        truncation = _optional_bool(body.get("truncation"), "truncation")

        # Coarse budget accounting is deliberately based on input characters;
        # reranking has no generated token stream to charge.
        chars = len(query) + sum(len(document) for document in documents)
        budget_units = max(1, (chars + 3) // 4)
        return RerankRequest(
            query=query,
            documents=tuple(documents),
            source_documents=tuple(raw_documents),
            top_n=parsed_top,
            return_documents=bool(return_documents),
            max_tokens_per_doc=parsed_max_tokens,
            priority=priority,
            truncation=truncation,
            budget_units=budget_units,
        )

    @staticmethod
    def _payload(backend: RerankBackend, request: RerankRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": backend.model,
            "query": request.query,
            "documents": list(request.documents),
        }
        if request.top_n is not None:
            payload["top_k" if backend.style == "voyage" else "top_n"] = request.top_n
        if backend.style == "cohere":
            if request.max_tokens_per_doc is not None:
                payload["max_tokens_per_doc"] = request.max_tokens_per_doc
            if request.priority is not None:
                payload["priority"] = request.priority
        else:
            # Voyage and Jina accept the document inclusion switch. Keep it
            # provider-local because Cohere v2 no longer advertises that field.
            payload["return_documents"] = request.return_documents
            if request.truncation is not None and backend.style == "voyage":
                payload["truncation"] = request.truncation
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
                "Reranker provider is rate limited; retry shortly",
                code="rate_limit_exceeded",
                body=body,
                headers=headers,
            )
        if status == 401:
            error = ProviderError(
                "Reranker provider authentication failed",
                code="auth_error",
            )
        elif status == 403:
            error = ProviderError(
                "Reranker provider access forbidden",
                code="forbidden",
            )
        elif status >= 500:
            error = ProviderError(
                "Reranker provider returned a server error",
                code="provider_error",
            )
        else:
            error = ProviderError(
                "Reranker provider rejected the request",
                code="provider_error",
            )
        error.upstream_status = status
        error.upstream_body = body
        logger.warning(
            "rerank provider rejected request provider=%s model=%s status=%d",
            provider,
            model,
            status,
        )
        return error

    async def _call_backend(
        self,
        backend: RerankBackend,
        request: RerankRequest,
        session: aiohttp.ClientSession | Any | None,
    ) -> dict[str, Any]:
        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=_env_float(
                        "TUSKER_RERANKER_TIMEOUT_SECS",
                        _RERANK_TIMEOUT_SECS,
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
                        "TUSKER_RERANKER_TIMEOUT_SECS",
                        _RERANK_TIMEOUT_SECS,
                    )
                ),
            ) as response:
                raw_body = await response.text()
                if not 200 <= response.status < 300:
                    raise self._error_from_status(
                        response.status,
                        raw_body,
                        dict(getattr(response, "headers", {}) or {}),
                        backend.provider,
                        backend.model,
                    )
                try:
                    parsed = json.loads(raw_body)
                except (TypeError, ValueError) as exc:
                    error = ProviderError(
                        "Reranker provider returned invalid JSON",
                        code="provider_error",
                    )
                    error.upstream_status = response.status
                    error.upstream_body = "invalid_json"
                    raise error from exc
                if not isinstance(parsed, dict):
                    error = ProviderError(
                        "Reranker provider returned an invalid response",
                        code="provider_error",
                    )
                    error.upstream_status = response.status
                    error.upstream_body = "invalid_response_shape"
                    raise error
                return parsed
        except GatewayError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            error = ProviderError(
                "Reranker provider request failed",
                code="timeout" if isinstance(exc, TimeoutError) else "upstream_error",
            )
            error.upstream_status = 504 if isinstance(exc, TimeoutError) else None
            error.upstream_body = type(exc).__name__
            raise error from exc
        finally:
            if owns_session and session is not None:
                await session.close()

    @staticmethod
    def _normalize_response(
        raw: dict[str, Any],
        request: RerankRequest,
        model: str,
    ) -> dict[str, Any]:
        raw_results = raw.get("results")
        if not isinstance(raw_results, list):
            raw_results = raw.get("data")
        if not isinstance(raw_results, list):
            raw_results = raw.get("rankings")
        if not isinstance(raw_results, list):
            raise ProviderError(
                "Reranker provider returned no results list",
                code="provider_error",
            )

        results: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                raise ProviderError(
                    "Reranker provider returned an invalid result item",
                    code="provider_error",
                )
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise ProviderError(
                    "Reranker provider returned an invalid result index",
                    code="provider_error",
                )
            if index < 0 or index >= len(request.source_documents):
                raise ProviderError(
                    "Reranker provider returned an out-of-range result index",
                    code="provider_error",
                )
            score = item.get(
                "relevance_score",
                item.get("score", item.get("logit")),
            )
            try:
                score = float(score)
            except (TypeError, ValueError) as exc:
                raise ProviderError(
                    "Reranker provider returned an invalid relevance score",
                    code="provider_error",
                ) from exc
            result: dict[str, Any] = {
                "index": index,
                "relevance_score": score,
            }
            if request.return_documents:
                result["document"] = request.source_documents[index]
            results.append(result)

        output: dict[str, Any] = {
            key: value
            for key, value in raw.items()
            if key not in {"data", "rankings", "results", "model"}
        }
        output["model"] = raw.get("model") or model
        output["results"] = results
        return output

    def _mark_failure(
        self,
        backend: RerankBackend,
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
            _persist_cooldown(self.config, backend.provider, backend.model, seconds)
        if global_tracker().record_failure(backend.provider):
            provider_seconds = 300.0
            global_tracker().cooldown(backend.provider, "", provider_seconds)
            _persist_cooldown(self.config, backend.provider, "", provider_seconds)

    async def rerank(
        self,
        body: dict[str, Any],
        *,
        session: aiohttp.ClientSession | Any | None = None,
        breaker: Any | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        request = self.validate_request(body)
        backends, _ = self.backends_for_model(body.get("model"))
        if not backends:
            raise RerankerUnavailableError(
                "No reranker providers are configured; configure a Cohere, Voyage, or Jina API key",
                code="no_reranker_providers",
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
                # Validate the provider response before marking the backend
                # healthy. A 200 response with an unusable shape is still a
                # provider failure and must participate in fallback/cooldown.
                result = self._normalize_response(raw, request, backend.model)
                if breaker is not None:
                    breaker.record_success(backend.provider, backend.model)
                global_tracker().clear_failures(backend.provider)
                with self._lock:
                    self._cursor = (start + position + 1) % len(backends)
                logger.info(
                    "rerank completed provider=%s model=%s documents=%d results=%d",
                    backend.provider,
                    backend.model,
                    len(request.documents),
                    len(result["results"]),
                )
                return backend.provider, backend.model, result
            except GatewayError as exc:
                last_error = exc
                self._mark_failure(backend, exc, breaker)
                logger.warning(
                    "rerank backend failed provider=%s model=%s error=%s",
                    backend.provider,
                    backend.model,
                    exc.code or type(exc).__name__,
                )
                continue

        if last_error is not None:
            raise last_error
        if not attempted:
            raise NoHealthyModelsError(pool="rerank")
        raise RerankerUnavailableError(
            "All reranker providers are temporarily unavailable; retry shortly",
            code="reranker_unavailable",
        )


__all__ = [
    "RerankHandler",
    "RerankRequest",
    "RerankBackend",
    "RerankerUnavailableError",
]
