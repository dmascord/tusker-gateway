"""Authentication strategies per provider.

Each strategy turns a provider endpoint configuration into signed request headers.
This removes auth-specific branching from PassthroughClient.
"""
from __future__ import annotations

import base64
import json
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
        # Try static token from provider_api_keys first (e.g. gho_ token),
        # then fall back to CodexTokenRotator for OAuth device-code pools.
        raw_token: str | None = None
        provider_key = config.get("provider_api_keys", {}).get(provider.lower())
        if provider_key:
            raw_token = provider_key
        elif self._rotator:
            raw_token = await self._rotator.get_token()

        if raw_token:
            try:
                token, _ = await exchange_copilot_token(raw_token, base_url=endpoint.base_url)
                headers["Authorization"] = f"Bearer {token}"
            except ValueError:
                # Exchange failed — use raw token directly (may work for some providers)
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


class CodexAuthenticator(Authenticator):
    """Codex Responses API authenticator.

    Uses the OAuth JWT directly with Cloudflare bypass headers.
    Does NOT exchange via the Copilot token exchange endpoint.
    """

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
        # Try static token from provider_api_keys first, then rotator pool
        raw_token: str | None = None
        provider_key = config.get("provider_api_keys", {}).get(provider.lower())
        if provider_key:
            raw_token = provider_key
        elif self._rotator:
            raw_token = await self._rotator.get_token()

        if raw_token:
            headers["Authorization"] = f"Bearer {raw_token}"

        # Cloudflare bypass headers for chatgpt.com/backend-api/codex
        headers["originator"] = "codex_cli_rs"
        headers["User-Agent"] = "codex_cli_rs/0.0.0 (Tusker Gateway)"
        # Extract ChatGPT-Account-ID from JWT if possible
        if raw_token and isinstance(raw_token, str):
            try:
                parts = raw_token.split(".")
                if len(parts) >= 2:
                    payload = parts[1] + "=="  # add padding
                    claims = json.loads(base64.urlsafe_b64decode(payload))
                    acct_id = claims.get("chatgpt_account_id")
                    if acct_id:
                        headers["ChatGPT-Account-ID"] = str(acct_id)
            except Exception:
                pass

        if endpoint.model_header:
            headers[endpoint.model_header] = model
        return headers


# Map auth_type -> strategy class (instantiated per request)
AUTH_STRATEGIES: dict[str, type[Authenticator]] = {
    "bearer": BearerAuthenticator,
    "oauth": OAuthAuthenticator,
    "codex": CodexAuthenticator,
}


def get_auth_strategy(auth_type: str, rotator: CodexTokenRotator | None = None) -> Authenticator:
    """Return an authenticator instance for the requested auth type."""
    if auth_type == "oauth":
        if rotator is None:
            raise RuntimeError("OAuth provider requires CodexTokenRotator")
        return OAuthAuthenticator(rotator)
    if auth_type == "codex":
        if rotator is None:
            raise RuntimeError("Codex provider requires CodexTokenRotator")
        return CodexAuthenticator(rotator)
    return BearerAuthenticator()
