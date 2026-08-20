"""Provider client and passthrough dispatch.

Handles the actual HTTP call to upstream providers, with token rotation
for Codex OAuth and cooldown tracking on failures.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

import aiohttp

from tusker_gateway.cooldown import global_tracker
from tusker_gateway.errors import (
    GatewayError,
    ProviderError,
    RateLimitError,
)
from tusker_gateway.quality import QualityDB

# Known OAuth-based providers that require token rotation.
OAUTH_PROVIDERS = {"github-copilot", "github-copilot-enterprise", "openai-codex"}

# Per-provider base URLs and auth patterns.
PROVIDER_ENDPOINTS: dict[str, dict[str, Any]] = {
    "github-copilot": {
        "base_url": "https://api.githubcopilot.com",
        "chat_path": "/chat/completions",
        "auth_type": "oauth",
        "model_header": "x-github-gpt-model",
    },
    "github-copilot-enterprise": {
        "base_url": "https://api.githubcopilot.com",
        "chat_path": "/chat/completions",
        "auth_type": "oauth",
        "model_header": "x-github-gpt-model",
    },
    "openai-codex": {
        "base_url": "https://api.github.com/copilot",
        "chat_path": "/chat/completions",
        "auth_type": "oauth",
        "model_header": "x-openai-gpt-model",
    },
    "openai": {
        "base_url": "https://api.openai.com",
        "chat_path": "/v1/chat/completions",
        "auth_type": "bearer",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "chat_path": "/chat/completions",
        "auth_type": "bearer",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai",
        "chat_path": "/v1/chat/completions",
        "auth_type": "bearer",
    },
    "local-llm": {
        "base_url": "http://localhost:11434",
        "chat_path": "/v1/chat/completions",
        "auth_type": "bearer",
    },
    "zai": {
        "base_url": "https://api.z.ai/api/paas",
        "chat_path": "/v4/chat/completions",
        "auth_type": "bearer",
    },
}


def _creds_access_token(cred: dict[str, Any]) -> str | None:
    """Return the access token from a credential, handling both formats."""
    return cred.get("access_token") or cred.get("token")


def _creds_refresh_token(cred: dict[str, Any]) -> str | None:
    """Return the refresh token from a credential."""
    return cred.get("refresh_token")


def _creds_expires_at(cred: dict[str, Any]) -> float:
    """Return credential expiry as epoch seconds (handles expires_at & expires_at_ms)."""
    ms = cred.get("expires_at_ms")
    if ms:
        return float(ms) / 1000.0
    return float(cred.get("expires_at", 0) or 0)


class CodexTokenRotator:
    """Rotates Codex OAuth tokens across a credential pool.

    Supports Hermes-format credentials (access_token, refresh_token, expires_at_ms)
    and legacy format (token, refresh_token, expires_at).

    When a token is near expiry, ``get_token()`` automatically attempts a
    refresh via the Copilot token-exchange endpoint and persists the updated
    credential back to the auth file if one is configured.
    """

    _JWT_REFRESH_MARGIN_SECONDS = 120

    def __init__(
        self,
        credentials: list[dict[str, Any]],
        *,
        auth_file: str | None = None,
        http_client: Any | None = None,
    ):
        self._creds: list[dict[str, Any]] = list(credentials)
        self._index = 0
        self._lock = asyncio.Lock()
        self._auth_file = auth_file
        self._http = http_client  # aiohttp.ClientSession for exchange

    @property
    def size(self) -> int:
        return len(self._creds)

    def reload(self, credentials: list[dict[str, Any]]) -> None:
        """Reload the pool from external source (e.g. file)."""
        self._creds = list(credentials)
        self._index = min(self._index, max(len(self._creds) - 1, 0))

    async def get_token(self) -> str | None:
        """Return the current active token, or None if no credentials.

        If the token is near expiry, attempts an automatic refresh.
        """
        if not self._creds:
            return None
        async with self._lock:
            cred = self._creds[self._index % len(self._creds)]
            if self._http and self._is_near_expiry(cred):
                try:
                    cred = await self._refresh_one(cred)
                    self._creds[self._index % len(self._creds)] = cred
                    self._persist()
                except Exception:
                    pass  # best-effort refresh
            return _creds_access_token(cred)

    async def advance(self) -> None:
        """Move to the next credential in the pool."""
        if len(self._creds) > 1:
            async with self._lock:
                self._index = (self._index + 1) % len(self._creds)

    async def refresh_if_needed(self, cred: dict[str, Any]) -> dict[str, Any]:
        """Check token expiry and refresh if needed."""
        if self._is_near_expiry(cred):
            try:
                return await self._refresh_one(cred)
            except Exception:
                return cred
        return cred

    async def _refresh_one(self, cred: dict[str, Any]) -> dict[str, Any]:
        """Exchange the raw (refresh) token for a new API token."""
        from tusker_gateway.copilot_exchange import exchange_copilot_token

        refresh = _creds_refresh_token(cred)
        if not refresh:
            return cred

        # Use the credential's host (GHE) if set
        base_url = None
        host = cred.get("host")
        if host and host not in ("github.com",):
            base_url = f"https://{host}/copilot"

        try:
            token, expires_at = await exchange_copilot_token(refresh, base_url=base_url, http=self._http)
            cred["access_token"] = token
            cred["expires_at_ms"] = int(expires_at * 1000)
            return cred
        except ValueError:
            return cred

    @classmethod
    def _is_near_expiry(cls, cred: dict[str, Any]) -> bool:
        expires_at = _creds_expires_at(cred)
        return bool(expires_at and time.time() >= expires_at - cls._JWT_REFRESH_MARGIN_SECONDS)

    def _persist(self) -> None:
        """Write the current pool back to the auth file (Hermes format)."""
        if not self._auth_file:
            return
        try:
            from tusker_gateway.copilot_enroll import save_auth_file
            save_auth_file(self._creds, self._auth_file)
        except Exception:
            pass  # best-effort persistence


class PassthroughClient:
    """HTTP client for provider passthrough requests."""

    def __init__(
        self,
        config: dict[str, Any],
        quality_db: QualityDB,
        http_client: aiohttp.ClientSession,
    ):
        self._config = config
        self._quality = quality_db
        self._http = http_client
        codex_creds = config.get("codex_credentials", [])
        # Determine auth file path
        auth_file = config.get("auth_file")
        if not auth_file:
            import os
            auth_file = os.getenv("TUSKER_AUTH_FILE")
        if not auth_file:
            from pathlib import Path
            auth_file = str(Path.home() / ".hermes" / "auth.json")
        self._codex_rotator = CodexTokenRotator(codex_creds, auth_file=auth_file, http_client=http_client)

    async def chat(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        *,
        stream: bool = False,
        api_key: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        upstream_gateway: str | None = None,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        """Make a passthrough chat completions call to the provider."""
        if upstream_gateway:
            endpoint = {"base_url": upstream_gateway.rstrip("/"), "chat_path": "/v1/chat/completions", "auth_type": "bearer"}
        else:
            endpoint = PROVIDER_ENDPOINTS.get(provider)
            if not endpoint:
                raise ProviderError(f"Unknown provider: {provider}")

        base_url = endpoint["base_url"]
        path = endpoint["chat_path"]
        url = f"{base_url}{path}"
        headers, body = await self._build_request(
            provider, model, messages,
            stream=stream, api_key=(self._config["api_keys"][0] if upstream_gateway else api_key),
            tools=tools,
            extra_headers=extra_headers, extra_body=extra_body,
            endpoint=endpoint,
        )

        start = time.monotonic()
        if stream:
            resp = await self._http.request(
                "POST", url, headers=headers, json=body,
                timeout=aiohttp.ClientTimeout(total=120),
            )
            try:
                await self._check_response(resp)
            except Exception:
                resp.release()
                raise
            return self._stream_events(resp)
        try:
            async with self._http.request(
                "POST", url, headers=headers, json=body,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                await self._check_response(resp)
                result = await resp.json()
                from tusker_gateway.tool_formats import normalize_response_tool_calls
                result = normalize_response_tool_calls(result)
                latency_ms = (time.monotonic() - start) * 1000
                await self._record_quality(provider, model, True, latency_ms)
                return result
        except RateLimitError as exc:
            from tusker_gateway.cooldown import _cooldown_seconds_for_429
            tracker = global_tracker()
            body_text = exc.body or "429 rate limit"
            seconds = _cooldown_seconds_for_429({"body": body_text, "headers": {}})
            tracker.cooldown(provider, model, seconds)
            try:
                from tusker_gateway.persistent_cooldown import PersistentCooldownStore
                from pathlib import Path
                db_path = Path(self._config.get("quality_db_path", "data/quality.db")).parent / "cooldowns.db"
                store = PersistentCooldownStore(db_path=db_path)
                store.record(provider, model, seconds)
            except Exception:
                pass
            raise
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            await self._record_quality(provider, model, False, latency_ms)
            raise ProviderError(str(exc)) from exc

    async def _build_request(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        *,
        stream: bool,
        api_key: str | None,
        tools: list[dict[str, Any]] | None = None,
        extra_headers: dict[str, str] | None,
        extra_body: dict[str, Any] | None,
        endpoint: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        from tusker_gateway.auth_strategies import get_auth_strategy
        from tusker_gateway.models import ProviderConfig
        from tusker_gateway.tool_formats import normalize_tools

        endpoint_model = ProviderConfig.from_raw(endpoint)
        strategy = get_auth_strategy(endpoint_model.auth_type, self._codex_rotator)
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            **(extra_headers or {}),
        }
        headers.update(
            await strategy.headers(
                self._config, provider, model, api_key, endpoint_model
            )
        )
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if tools:
            body["tools"] = normalize_tools(tools)
        if extra_body:
            body.update(extra_body)
        return headers, body

    @staticmethod
    async def _check_response(resp: aiohttp.ClientResponse) -> None:
        if resp.status == 200:
            return
        body = await resp.text()
        if resp.status == 401:
            raise ProviderError("Provider authentication failed", code="auth_error")
        if resp.status == 403:
            raise ProviderError("Provider access forbidden", code="forbidden")
        if resp.status == 429:
            raise RateLimitError(body=body)
        if resp.status >= 500:
            raise ProviderError(f"Provider returned {resp.status}: {body[:200]}", code="provider_error")
        if resp.status != 200:
            raise ProviderError(f"Provider returned {resp.status}: {body[:200]}", code="provider_error")

    async def _stream_events(self, resp: aiohttp.ClientResponse) -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.content.iter_any():
                yield chunk
        except aiohttp.ClientConnectionError:
            pass  # client disconnect during streaming
        finally:
            resp.release()

    async def _record_quality(
        self, provider: str, model: str, success: bool, latency_ms: float
    ) -> None:
        try:
            if hasattr(self._quality, "record"):
                await self._quality.record(provider, model, success, latency_ms)
        except Exception:
            pass  # Quality DB is best-effort
