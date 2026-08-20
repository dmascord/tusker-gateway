"""GitHub Copilot token exchange + enterprise-aware header injection.

Replicates the public/enterprise auth flows used by hermes-agent:
- exchange raw `gho_*` / `ghu_*` / `github_pat_*` for short-lived API tokens
- derive enterprise exchange URL from `.ghe.com` / `api.*` hosts
- emit the canonical Copilot header set (Editor-Version, Copilot-Integration-Id,
  Openai-Intent, x-initiator, Copilot-Vision-Request)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp

from tusker_gateway.copilot_constants import (
    EDITOR_VERSION as _EDITOR_VERSION,
    EXCHANGE_USER_AGENT as _EXCHANGE_USER_AGENT,
    JWT_REFRESH_MARGIN_SECONDS,
    PUBLIC_EXCHANGE_URL,
)

# In-process cache: raw_token_fingerprint -> (api_token, expires_at_epoch)
_jwt_cache: dict[str, tuple[str, float]] = {}
_cache_lock = asyncio.Lock()


def _derive_enterprise_exchange_url(base_url: str | None) -> str | None:
    """Derive a GHE token-exchange URL from a Copilot Enterprise base URL."""
    b = str(base_url or "").strip().rstrip("/")
    if not b:
        return None
    try:
        parsed = urlparse(b)
        host = (parsed.hostname or "").lower()
        if not host:
            return None
        if host.startswith("copilot-api."):
            host = "api." + host[len("copilot-api.") :]
        if host in {"api.githubcopilot.com", "api.github.com"}:
            return None
        if host.endswith(".ghe.com") or host.startswith("api."):
            return f"https://{host}/copilot_internal/v2/token"
    except Exception:
        return None
    return None


def _token_fingerprint(raw_token: str, exchange_url: str | None) -> str:
    return hashlib.sha256(f"{raw_token}|{exchange_url or 'public'}".encode()).hexdigest()[:16]


async def exchange_copilot_token(
    raw_token: str,
    *,
    base_url: str | None = None,
    http: aiohttp.ClientSession | None = None,
    timeout: float = 10.0,
) -> tuple[str, float]:
    """Exchange a raw GitHub token for a short-lived Copilot API token.

    Tries the public endpoint first, then the enterprise-derived endpoint if applicable.
    Returns (api_token, expires_at_epoch). Caches in-process until close to expiry.
    Raises ValueError on failure.
    """
    enterprise_exchange = _derive_enterprise_exchange_url(base_url)
    exchange_candidates: list[str] = [PUBLIC_EXCHANGE_URL]
    if enterprise_exchange and enterprise_exchange not in exchange_candidates:
        exchange_candidates.append(enterprise_exchange)

    fp = _token_fingerprint(raw_token, enterprise_exchange)

    # Check cache first
    async with _cache_lock:
        cached = _jwt_cache.get(fp)
    if cached:
        api_token, expires_at = cached
        if time.time() < expires_at - JWT_REFRESH_MARGIN_SECONDS:
            return api_token, expires_at

    owns_session = http is None
    if owns_session:
        http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))

    last_exc: Exception | None = None
    try:
        for exchange_url in exchange_candidates:
            try:
                async with http.get(
                    exchange_url,
                    headers={
                        "Authorization": f"token {raw_token}",
                        "User-Agent": _EXCHANGE_USER_AGENT,
                        "Accept": "application/json",
                        "Editor-Version": _EDITOR_VERSION,
                    },
                ) as resp:
                    if resp.status != 200:
                        last_exc = ValueError(f"HTTP {resp.status}")
                        continue
                    data = await resp.json()
            except Exception as exc:
                last_exc = exc
                continue

            api_token = data.get("token", "")
            expires_at = float(data.get("expires_at", 0) or 0)
            if not api_token:
                last_exc = ValueError("empty token in response")
                continue
            if not expires_at:
                expires_at = time.time() + 1800

            async with _cache_lock:
                _jwt_cache[fp] = (api_token, expires_at)
            return api_token, expires_at
    finally:
        if owns_session:
            await http.close()

    raise ValueError(f"Copilot token exchange failed: {last_exc}")


def copilot_request_headers(
    *,
    is_agent_turn: bool = True,
    is_vision: bool = False,
    base_url: str | None = None,
    integration_id_override: str | None = None,
    user_agent: str = "HermesAgent/1.0",
) -> dict[str, str]:
    """Build the standard Copilot API request headers.

    Picks `vscode-chat` or `copilot-developer-cli` integration id based on base URL,
    matching hermes-agent behaviour.
    """
    import os

    normalized = str(base_url or "").strip().rstrip("/").lower()
    if integration_id_override:
        integration_id = integration_id_override
    else:
        default = "vscode-chat"
        if "copilot-api." in normalized or normalized.startswith("https://api.sita.ghe.com"):
            default = "copilot-developer-cli"
        integration_id = os.getenv("GITHUB_COPILOT_INTEGRATION_ID", default)

    headers: dict[str, str] = {
        "Editor-Version": _EDITOR_VERSION,
        "User-Agent": user_agent,
        "Copilot-Integration-Id": integration_id,
        "Openai-Intent": "conversation-edits",
        "x-initiator": "agent" if is_agent_turn else "user",
    }
    if is_vision:
        headers["Copilot-Vision-Request"] = "true"
    return headers
