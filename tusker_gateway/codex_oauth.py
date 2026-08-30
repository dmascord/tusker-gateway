"""OpenAI Codex OAuth token refresh.

Codex ChatGPT OAuth credentials use a different token authority from GitHub
Copilot credentials.  Keep this small protocol implementation separate so a
Codex refresh token is never sent to the Copilot exchange endpoint.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import time
from typing import Any

import aiohttp


CODEX_REFRESH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class CodexOAuthError(ValueError):
    """A redacted, classified Codex OAuth refresh failure."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


def _configured_value(*names: str, default: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _jwt_exp(token: str) -> float | None:
    """Read only the untrusted expiry claim from a JWT-shaped token."""
    if token.count(".") != 2:
        return None
    try:
        encoded = token.split(".", 2)[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        value = payload.get("exp")
        return float(value) if value is not None else None
    except (
        TypeError,
        ValueError,
        KeyError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        return None


def _error_code(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    if isinstance(error, str):
        return error
    return data.get("code") if isinstance(data.get("code"), str) else None


def _expiry_from_response(data: dict[str, Any], access_token: str) -> float:
    jwt_exp = _jwt_exp(access_token)
    if jwt_exp and jwt_exp > time.time():
        return jwt_exp
    for key in ("expires_at",):
        value = data.get(key)
        try:
            if value is not None and float(value) > time.time():
                return float(value)
        except (TypeError, ValueError):
            pass
    try:
        expires_in = float(data.get("expires_in", 0) or 0)
    except (TypeError, ValueError):
        expires_in = 0
    return time.time() + expires_in if expires_in > 0 else time.time() + 1800


async def refresh_codex_token(
    refresh_token: str,
    *,
    http: aiohttp.ClientSession | None = None,
    timeout: float = 10.0,
) -> tuple[dict[str, Any], float]:
    """Refresh a ChatGPT Codex OAuth credential.

    Returns ``(token_response, access_token_expiry_epoch)``.  The response
    dict may contain a rotated ``refresh_token``; callers must persist it.
    """
    if not refresh_token:
        raise CodexOAuthError("Codex OAuth refresh token is missing", code="missing_refresh_token")

    endpoint = _configured_value(
        "TUSKER_CODEX_REFRESH_TOKEN_URL",
        "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
        default=CODEX_REFRESH_TOKEN_URL,
    )
    client_id = _configured_value(
        "TUSKER_CODEX_OAUTH_CLIENT_ID",
        "CODEX_APP_SERVER_LOGIN_CLIENT_ID",
        default=CODEX_OAUTH_CLIENT_ID,
    )
    payload = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "codex_cli_rs/0.0.0 (Tusker Gateway)",
    }

    owns_session = http is None
    session = http
    if owns_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))

    try:
        try:
            async with session.post(  # type: ignore[union-attr]
                endpoint,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                status = int(response.status)
                try:
                    body = await response.text()
                    data = json.loads(body) if body else {}
                except (AttributeError, json.JSONDecodeError):
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        data = {}

                if status < 200 or status >= 300:
                    code = _error_code(data)
                    retryable = status == 429 or status >= 500
                    raise CodexOAuthError(
                        "Codex OAuth refresh rejected",
                        status=status,
                        code=code,
                        retryable=retryable,
                    )
                if not isinstance(data, dict):
                    raise CodexOAuthError("Codex OAuth refresh returned invalid JSON")
                access_token = data.get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise CodexOAuthError(
                        "Codex OAuth refresh returned no access token",
                        status=status,
                        code=_error_code(data),
                    )
                return data, _expiry_from_response(data, access_token)
        except CodexOAuthError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise CodexOAuthError(
                "Codex OAuth refresh transport failure",
                retryable=True,
            ) from exc
    finally:
        if owns_session and session is not None:
            await session.close()
