"""Authentication strategies per provider.

Each strategy turns a provider endpoint configuration into signed request headers.
This removes auth-specific branching from PassthroughClient.
"""
from __future__ import annotations

from typing import Any

from tusker_gateway.copilot_constants import EDITOR_VERSION, EXCHANGE_USER_AGENT, is_likely_vision_model
from tusker_gateway.copilot_exchange import copilot_request_headers, exchange_copilot_token
from tusker_gateway.models import ProviderConfig
from tusker_gateway.passthrough import CodexTokenRotator


class Authenticator:
    """Base interface. Implement `headers` for an auth type."""

    async def headers(
        self,
        config: dict[str, Any],
        provider: str,
        model: str,
        api_key: str | None,
        endpoint: ProviderConfig,
    ) -> dict[str, str]:
        raise NotImplementedError


class BearerAuthenticator(Authenticator):
    """Standard bearer token from config provider_api_keys or explicit api_key."""

    async def headers(
        self,
        config: dict[str, Any],
        provider: str,
        model: str,
        api_key: str | None,
        endpoint: ProviderConfig,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        provider_key = config.get("provider_api_keys", {}).get(provider.lower())
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        elif provider_key:
            headers["Authorization"] = f"Bearer {provider_key}"
        return headers


class OAuthAuthenticator(Authenticator):
    """Copilot/Enterprise OAuth exchange using CodexTokenRotator."""

    def __init__(self, rotator: CodexTokenRotator) -> None:
        self._rotator = rotator

    async def headers(
        self,
        config: dict[str, Any],
        provider: str,
        model: str,
        api_key: str | None,
        endpoint: ProviderConfig,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        raw_token = await self._rotator.get_token()
        if raw_token:
            try:
                token, _ = await exchange_copilot_token(raw_token, base_url=endpoint.base_url)
                headers["Authorization"] = f"Bearer {token}"
            except ValueError:
                headers["Authorization"] = f"Bearer {raw_token}"

        headers.update(
            copilot_request_headers(
                base_url=endpoint.base_url,
                is_vision=is_likely_vision_model(model),
            )
        )
        if endpoint.model_header:
            headers[endpoint.model_header] = model
        return headers


# Map auth_type -> strategy class (instantiated per request)
AUTH_STRATEGIES: dict[str, type[Authenticator]] = {
    "bearer": BearerAuthenticator,
    "oauth": OAuthAuthenticator,
}


def get_auth_strategy(auth_type: str, rotator: CodexTokenRotator | None = None) -> Authenticator:
    """Return an authenticator instance for the requested auth type."""
    if auth_type == "oauth":
        if rotator is None:
            raise RuntimeError("OAuth provider requires CodexTokenRotator")
        return OAuthAuthenticator(rotator)
    return BearerAuthenticator()
