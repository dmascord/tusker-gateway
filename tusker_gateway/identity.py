"""Caller identity and authorization policy for gateway API keys.

Raw API keys remain in ``API_KEYS``.  Enterprise identity metadata is keyed by
the SHA-256 fingerprint of a key so policy configuration can be reviewed or
stored without duplicating the credential itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import hashlib
import json
import logging
import os
from typing import Any, Mapping

from aiohttp import web

from tusker_gateway.errors import AuthorizationError, BadRequestError
from tusker_gateway.routing import resolve_route

logger = logging.getLogger(__name__)

_WILDCARD = ("*",)
_ROUTE_SCOPES = {
    ("GET", "/status"): "status:read",
    ("GET", "/v1/models"): "models:read",
    ("POST", "/v1/chat/completions"): "inference:chat",
    ("POST", "/v1/responses"): "inference:chat",
    ("POST", "/v1/messages"): "inference:chat",
    ("POST", "/v1/images/generations"): "inference:images",
    ("POST", "/v1/images/edits"): "inference:images",
    ("POST", "/v1/images/variations"): "inference:images",
    ("POST", "/v1/audio/speech"): "inference:audio",
    ("POST", "/v1/videos"): "inference:video",
    ("POST", "/v1/rerank"): "inference:rerank",
}
_CHAT_ROUTES = frozenset({
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/messages",
})


def fingerprint_api_key(api_key: str) -> str:
    """Return the stable, non-secret identifier used by policy and quotas."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def extract_api_key(request: web.Request) -> str:
    """Extract OpenAI or Anthropic client authentication from a request."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    return request.headers.get("x-api-key", "").strip()


def _patterns(value: Any, *, default: tuple[str, ...] = _WILDCARD) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("policy allowlists must be strings or arrays of strings")
    result = tuple(str(item).strip() for item in value if str(item).strip())
    if len(result) > 100 or any(len(item) > 256 for item in result):
        raise ValueError("policy allowlists support at most 100 patterns of 256 characters")
    return result


@dataclass(frozen=True)
class CallerIdentity:
    """Authenticated caller metadata and least-privilege allowlists."""

    key_fingerprint: str
    principal: str
    tenant: str
    scopes: tuple[str, ...] = _WILDCARD
    allowed_pools: tuple[str, ...] = _WILDCARD
    allowed_models: tuple[str, ...] = _WILDCARD
    allowed_providers: tuple[str, ...] = _WILDCARD
    managed: bool = True

    @classmethod
    def from_raw(cls, fingerprint: str, raw: Mapping[str, Any]) -> "CallerIdentity":
        principal = str(raw.get("principal") or "").strip()
        tenant = str(raw.get("tenant") or "").strip()
        if not principal or not tenant:
            raise ValueError("identity profiles require non-empty principal and tenant")
        if len(principal) > 128 or len(tenant) > 128:
            raise ValueError("identity principal and tenant must be at most 128 characters")
        return cls(
            key_fingerprint=fingerprint,
            principal=principal,
            tenant=tenant,
            scopes=_patterns(raw.get("scopes")),
            allowed_pools=_patterns(raw.get("allowed_pools")),
            allowed_models=_patterns(raw.get("allowed_models")),
            allowed_providers=_patterns(raw.get("allowed_providers")),
        )

    @classmethod
    def legacy(cls, fingerprint: str) -> "CallerIdentity":
        return cls(
            key_fingerprint=fingerprint,
            principal=f"key:{fingerprint[:12]}",
            tenant="default",
            managed=False,
        )

    @staticmethod
    def _allows(patterns: tuple[str, ...], value: str) -> bool:
        return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)

    def allows_scope(self, scope: str) -> bool:
        return self._allows(self.scopes, scope)

    def allows_pool(self, pool: str) -> bool:
        return self._allows(self.allowed_pools, pool)

    def allows_model(self, model: str) -> bool:
        return self._allows(self.allowed_models, model)

    def allows_provider(self, provider: str) -> bool:
        return self._allows(self.allowed_providers, provider)


@dataclass(frozen=True)
class IdentityConfig:
    identities: dict[str, CallerIdentity] = field(default_factory=dict)
    required: bool = False


class IdentityStore:
    """Resolve API-key fingerprints to configured identities."""

    def __init__(self, config: IdentityConfig | None = None):
        self.config = config or IdentityConfig()

    def resolve(self, api_key: str) -> CallerIdentity:
        fingerprint = fingerprint_api_key(api_key)
        identity = self.config.identities.get(fingerprint)
        if identity is not None:
            return identity
        if self.config.required:
            raise AuthorizationError(
                "Authenticated API key has no enterprise identity profile",
                code="identity_profile_required",
            )
        return CallerIdentity.legacy(fingerprint)


def load_identity_config_from_env(
    env: Mapping[str, str] | None = None,
) -> IdentityConfig:
    env = os.environ if env is None else env
    required = env.get("TUSKER_IDENTITY_REQUIRED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    raw = env.get("TUSKER_IDENTITIES_JSON", "").strip()
    if not raw:
        if required:
            raise ValueError(
                "TUSKER_IDENTITY_REQUIRED is enabled but TUSKER_IDENTITIES_JSON is empty"
            )
        return IdentityConfig(required=False)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("TUSKER_IDENTITIES_JSON must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("TUSKER_IDENTITIES_JSON must be an object keyed by key fingerprint")

    identities: dict[str, CallerIdentity] = {}
    for fingerprint, profile in parsed.items():
        normalized = str(fingerprint).strip().lower()
        if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
            raise ValueError("identity profile keys must be 64-character SHA-256 fingerprints")
        if not isinstance(profile, dict):
            raise ValueError(f"identity profile {normalized[:12]} must be an object")
        identities[normalized] = CallerIdentity.from_raw(normalized, profile)
    return IdentityConfig(identities=identities, required=required)


def _deny(message: str, code: str) -> None:
    raise AuthorizationError(message, code=code)


def authorize_request_body(request: web.Request, body: Mapping[str, Any]) -> None:
    """Authorize an inspected body, including any trusted guardrail rewrite."""
    identity = request.get("identity")
    if not isinstance(identity, CallerIdentity):
        return

    requested_model = body.get("model")
    model = str(requested_model).strip() if requested_model is not None else ""
    if request.path in _CHAT_ROUTES:
        route = resolve_route(model or None, dict(body))
        pool = route.pool_name or ("code" if route.kind == "code" else None)
        provider = route.provider
        model_candidates = tuple(
            candidate
            for candidate in (
                model or "hermes-code",
                route.model,
                f"{route.provider}/{route.model}" if route.provider and route.model else None,
                f"{route.provider}::{route.model}" if route.provider and route.model else None,
            )
            if candidate
        )
    else:
        pool = "rerank" if request.path == "/v1/rerank" else "media"
        raw_provider = body.get("provider")
        provider = str(raw_provider).strip() if raw_provider is not None else None
        if provider is None and model:
            provider = resolve_route(model, dict(body)).provider
        model_candidates = (model,) if model else ()
    if pool and not identity.allows_pool(pool):
        _deny("Caller is not authorized for the requested model pool", "pool_not_allowed")
    if provider and not identity.allows_provider(provider):
        _deny("Caller is not authorized for the requested provider", "provider_not_allowed")
    if (
        request.path not in _CHAT_ROUTES
        and identity.allowed_providers != _WILDCARD
        and not provider
    ):
        _deny(
            "An explicit provider is required by the caller's provider policy",
            "provider_required_by_policy",
        )
    if model_candidates and not any(
        identity.allows_model(candidate) for candidate in model_candidates
    ):
        _deny("Caller is not authorized for the requested model", "model_not_allowed")


async def authorize_request(request: web.Request) -> None:
    """Enforce route, pool, provider, and model policy for a caller."""
    identity = request.get("identity")
    if not isinstance(identity, CallerIdentity):
        return

    required_scope = _ROUTE_SCOPES.get((request.method, request.path))
    if required_scope and not identity.allows_scope(required_scope):
        _deny("Caller is not authorized for this API capability", "insufficient_scope")

    if request.method != "POST" or not request.path.startswith("/v1/"):
        return
    if not any(
        patterns != _WILDCARD
        for patterns in (
            identity.allowed_pools,
            identity.allowed_models,
            identity.allowed_providers,
        )
    ):
        return

    if request.path not in _CHAT_ROUTES:
        logical_pool = "rerank" if request.path == "/v1/rerank" else "media"
        if not identity.allows_pool(logical_pool):
            _deny(
                "Caller is not authorized for the requested model pool",
                "pool_not_allowed",
            )

    is_json = request.content_type == "application/json" or request.content_type.endswith(
        "+json"
    )
    if not is_json:
        if (
            identity.allowed_models != _WILDCARD
            or identity.allowed_providers != _WILDCARD
        ):
            _deny(
                "Model/provider policy cannot inspect this request content type",
                "request_policy_uninspectable",
            )
        return

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        # Let the endpoint return its normal request-shape error.
        return
    if not isinstance(body, dict):
        return

    authorize_request_body(request, body)


def provider_patterns_for_request(request: web.Request | None) -> tuple[str, ...] | None:
    """Return provider patterns that pool selection must enforce."""
    if request is None:
        return None
    getter = getattr(request, "get", None)
    if not callable(getter):
        return None
    identity = getter("identity")
    if not isinstance(identity, CallerIdentity):
        return None
    return None if identity.allowed_providers == _WILDCARD else identity.allowed_providers


def model_patterns_for_request(request: web.Request | None) -> tuple[str, ...] | None:
    """Return concrete-model patterns that pool selection must enforce."""
    if request is None:
        return None
    getter = getattr(request, "get", None)
    if not callable(getter):
        return None
    identity = getter("identity")
    if not isinstance(identity, CallerIdentity):
        return None
    return None if identity.allowed_models == _WILDCARD else identity.allowed_models


def pool_allowed_for_request(request: web.Request | None, pool: str) -> bool:
    """Return whether the caller may enter ``pool`` during fallback routing."""
    if request is None:
        return True
    getter = getattr(request, "get", None)
    if not callable(getter):
        return True
    identity = getter("identity")
    return not isinstance(identity, CallerIdentity) or identity.allows_pool(pool)


def model_allowed_for_request(
    request: web.Request | None,
    provider: str,
    model: str,
) -> bool:
    """Return whether a concrete provider/model pair satisfies caller policy."""
    patterns = model_patterns_for_request(request)
    if patterns is None:
        return True
    candidates = (model, f"{provider}/{model}", f"{provider}::{model}")
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for candidate in candidates
        for pattern in patterns
    )


def attach_authorization_middleware(app: web.Application) -> None:
    @web.middleware
    async def authorization_middleware(request: web.Request, handler):
        try:
            await authorize_request(request)
        except (AuthorizationError, BadRequestError) as exc:
            from tusker_gateway.errors import openai_error

            return web.json_response(
                openai_error(exc.message, code=exc.code, error_type=exc.error_type),
                status=exc.status,
            )
        return await handler(request)

    app.middlewares.append(authorization_middleware)


__all__ = [
    "CallerIdentity",
    "IdentityConfig",
    "IdentityStore",
    "attach_authorization_middleware",
    "authorize_request_body",
    "authorize_request",
    "extract_api_key",
    "fingerprint_api_key",
    "load_identity_config_from_env",
    "model_allowed_for_request",
    "model_patterns_for_request",
    "pool_allowed_for_request",
    "provider_patterns_for_request",
]
