"""OpenAI Codex OAuth issuance and token refresh.

Codex ChatGPT OAuth credentials use a different token authority from GitHub
Copilot credentials. Keep this small protocol implementation separate so a
Codex credential is never sent to the Copilot exchange endpoint.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp


CODEX_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
CODEX_REFRESH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_CALLBACK_PORT = 1455
CODEX_CALLBACK_PATH = "/auth/callback"
CODEX_DEFAULT_REDIRECT_URI = f"http://localhost:{CODEX_CALLBACK_PORT}{CODEX_CALLBACK_PATH}"
CODEX_SCOPE = "openid profile email offline_access"
CODEX_DEVICE_USERCODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
CODEX_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
CODEX_DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
CODEX_DEVICE_AUTH_URL = "https://auth.openai.com/codex/device"
CODEX_DEVICE_POLL_INTERVAL_SECONDS = 5.0
CODEX_DEVICE_POLL_SAFETY_MARGIN_SECONDS = 3.0
CODEX_DEVICE_MAX_POLLS = 120
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


class CodexOAuthError(ValueError):
    """A redacted, classified Codex OAuth failure."""

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


def _jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without treating it as authenticated data."""
    if token.count(".") != 2:
        return None
    try:
        encoded = token.split(".", 2)[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        return payload if isinstance(payload, dict) else None
    except (
        TypeError,
        ValueError,
        KeyError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        return None


def _jwt_exp(token: str) -> float | None:
    """Read only the untrusted expiry claim from a JWT-shaped token."""
    payload = _jwt_payload(token)
    if not payload:
        return None
    try:
        value = payload.get("exp")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def codex_token_profile(
    access_token: str,
    id_token: str | None = None,
) -> dict[str, str]:
    """Extract non-secret account metadata from native Codex JWTs.

    OpenAI has emitted account/profile claims both at the top level and under
    namespaced claims over time. The gateway only uses this for request
    headers, labels, and diagnostics; JWT signatures are not validated here.
    """
    profile: dict[str, str] = {}
    payloads = [_jwt_payload(access_token)]
    if id_token:
        payloads.append(_jwt_payload(id_token))

    for payload in payloads:
        if not payload:
            continue
        auth_claim = payload.get("https://api.openai.com/auth")
        auth = auth_claim if isinstance(auth_claim, dict) else {}
        profile_claim = payload.get("https://api.openai.com/profile")
        token_profile = profile_claim if isinstance(profile_claim, dict) else {}

        for key in ("chatgpt_account_id", "account_id"):
            value = payload.get(key) or auth.get(key)
            if isinstance(value, str) and value.strip():
                profile.setdefault("account_id", value.strip())
                break
        for key in ("email", "preferred_username"):
            value = payload.get(key) or token_profile.get(key)
            if isinstance(value, str) and value.strip():
                profile.setdefault("email", value.strip().lower())
                break
    return profile


def _error_code(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    if isinstance(error, str):
        return error
    return data.get("code") if isinstance(data.get("code"), str) else None


async def _response_data(response: Any) -> Any:
    """Read a JSON response without assuming a concrete aiohttp response."""
    try:
        body = await response.text()
    except (AttributeError, TypeError):
        try:
            return await response.json(content_type=None)
        except Exception:
            return {}
    if not body:
        try:
            return await response.json(content_type=None)
        except Exception:
            return {}
    try:
        return json.loads(body)
    except (TypeError, json.JSONDecodeError):
        try:
            return await response.json(content_type=None)
        except Exception:
            return {}


def _oauth_endpoint() -> str:
    return _configured_value(
        "TUSKER_CODEX_TOKEN_URL",
        "TUSKER_CODEX_REFRESH_TOKEN_URL",
        "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
        default=CODEX_REFRESH_TOKEN_URL,
    )


def _oauth_client_id() -> str:
    return _configured_value(
        "TUSKER_CODEX_OAUTH_CLIENT_ID",
        "CODEX_APP_SERVER_LOGIN_CLIENT_ID",
        default=CODEX_OAUTH_CLIENT_ID,
    )


def _classify_http_error(operation: str, status: int, data: Any) -> CodexOAuthError:
    return CodexOAuthError(
        f"Codex OAuth {operation} rejected",
        status=status,
        code=_error_code(data),
        retryable=status == 429 or status >= 500,
    )


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


def _pkce_pair() -> tuple[str, str]:
    """Generate an RFC 7636 S256 verifier/challenge pair."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_codex_authorization_url(
    state: str,
    code_challenge: str,
    *,
    redirect_uri: str = CODEX_DEFAULT_REDIRECT_URI,
    originator: str = "tusker-gateway",
) -> str:
    """Build the native Codex browser authorization URL."""
    params = {
        "response_type": "code",
        "client_id": _oauth_client_id(),
        "redirect_uri": redirect_uri,
        "scope": CODEX_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": originator,
    }
    return f"{CODEX_AUTHORIZE_URL}?{urlencode(params)}"


def new_codex_authorization_request(
    *,
    redirect_uri: str = CODEX_DEFAULT_REDIRECT_URI,
    originator: str = "tusker-gateway",
) -> tuple[str, str, str]:
    """Return ``(authorization_url, state, code_verifier)`` for browser login."""
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    return (
        build_codex_authorization_url(
            state,
            challenge,
            redirect_uri=redirect_uri,
            originator=originator,
        ),
        state,
        verifier,
    )


def _credential_from_token_response(
    data: dict[str, Any],
    *,
    label: str | None = None,
    source: str = "codex-oauth",
) -> dict[str, Any]:
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise CodexOAuthError("Codex OAuth token response has no access token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CodexOAuthError("Codex OAuth token response has no refresh token")

    id_token = data.get("id_token")
    id_token = id_token if isinstance(id_token, str) and id_token else None
    profile = codex_token_profile(access_token, id_token)
    response_account = data.get("account_id")
    if isinstance(response_account, str) and response_account.strip():
        profile["account_id"] = response_account.strip()
    response_email = data.get("email")
    if isinstance(response_email, str) and response_email.strip():
        profile["email"] = response_email.strip().lower()

    credential: dict[str, Any] = {
        "id": secrets.token_hex(3),
        "label": label or profile.get("email") or profile.get("account_id") or "codex-oauth",
        "auth_type": "oauth",
        "priority": 0,
        "source": source,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "base_url": CODEX_BASE_URL,
        "expires_at_ms": int(_expiry_from_response(data, access_token) * 1000),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_count": 0,
        "provider": "openai-codex",
    }
    if id_token:
        credential["id_token"] = id_token
    if profile.get("account_id"):
        credential["account_id"] = profile["account_id"]
    if profile.get("email"):
        credential["email"] = profile["email"]
    return credential


async def exchange_codex_authorization_code(
    code: str,
    code_verifier: str,
    *,
    redirect_uri: str = CODEX_DEFAULT_REDIRECT_URI,
    http: aiohttp.ClientSession | None = None,
    timeout: float = 10.0,
    label: str | None = None,
) -> dict[str, Any]:
    """Exchange a native Codex authorization code for a Hermes credential."""
    if not code or not code_verifier:
        raise CodexOAuthError("Codex OAuth authorization code or verifier is missing")
    payload = {
        "grant_type": "authorization_code",
        "client_id": _oauth_client_id(),
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "codex_cli_rs/0.0.0 (Tusker Gateway)",
    }
    owns_session = http is None
    session = http
    if owns_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
    try:
        try:
            async with session.post(  # type: ignore[union-attr]
                _oauth_endpoint(),
                headers=headers,
                data=urlencode(payload).encode("utf-8"),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                status = int(response.status)
                data = await _response_data(response)
                if status < 200 or status >= 300:
                    raise _classify_http_error("authorization-code exchange", status, data)
                if not isinstance(data, dict):
                    raise CodexOAuthError("Codex OAuth authorization-code response is invalid")
                return _credential_from_token_response(
                    data,
                    label=label,
                    source="codex-authorization-code",
                )
        except CodexOAuthError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise CodexOAuthError(
                "Codex OAuth authorization-code transport failure",
                retryable=True,
            ) from exc
    finally:
        if owns_session and session is not None:
            await session.close()


async def issue_codex_device_token(
    *,
    http: aiohttp.ClientSession | None = None,
    timeout: float = 10.0,
    max_polls: int = CODEX_DEVICE_MAX_POLLS,
    poll_interval_seconds: float | None = None,
    on_authorize: Any | None = None,
    on_progress: Any | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Issue a native Codex credential through OpenAI's headless device flow.

    ``on_authorize(url, user_code)`` is called after the code is issued. The
    callback is intentionally synchronous so the CLI can simply print it.
    """
    if max_polls <= 0:
        raise CodexOAuthError("Codex device authorization poll limit is invalid")
    owns_session = http is None
    session = http
    if owns_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
    try:
        try:
            async with session.post(  # type: ignore[union-attr]
                CODEX_DEVICE_USERCODE_URL,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={"client_id": _oauth_client_id()},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                status = int(response.status)
                data = await _response_data(response)
                if status < 200 or status >= 300:
                    raise _classify_http_error("device authorization", status, data)
                if not isinstance(data, dict):
                    raise CodexOAuthError("Codex device authorization response is invalid")

            device_auth_id = data.get("device_auth_id")
            user_code = data.get("user_code")
            if not isinstance(device_auth_id, str) or not device_auth_id:
                raise CodexOAuthError("Codex device authorization response has no device id")
            if not isinstance(user_code, str) or not user_code:
                raise CodexOAuthError("Codex device authorization response has no user code")

            try:
                server_interval = float(data.get("interval", CODEX_DEVICE_POLL_INTERVAL_SECONDS))
            except (TypeError, ValueError):
                server_interval = CODEX_DEVICE_POLL_INTERVAL_SECONDS
            delay = max(0.0, server_interval + CODEX_DEVICE_POLL_SAFETY_MARGIN_SECONDS)
            if poll_interval_seconds is not None:
                delay = max(0.0, float(poll_interval_seconds))
            if on_authorize:
                on_authorize(CODEX_DEVICE_AUTH_URL, user_code)
            if on_progress:
                on_progress("Waiting for Codex authorization…")

            for _ in range(max_polls):
                await asyncio.sleep(delay)
                async with session.post(  # type: ignore[union-attr]
                    CODEX_DEVICE_TOKEN_URL,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as poll_response:
                    poll_status = int(poll_response.status)
                    poll_data = await _response_data(poll_response)

                pending = poll_status in {403, 404}
                if isinstance(poll_data, dict):
                    pending = pending or poll_data.get("error") in {
                        "authorization_pending",
                        "pending",
                    }
                    if poll_data.get("error") == "slow_down":
                        delay += 5.0
                if pending:
                    continue
                if poll_status < 200 or poll_status >= 300:
                    raise _classify_http_error("device token polling", poll_status, poll_data)
                if not isinstance(poll_data, dict):
                    raise CodexOAuthError("Codex device token response is invalid")
                authorization_code = poll_data.get("authorization_code")
                code_verifier = poll_data.get("code_verifier")
                if not isinstance(authorization_code, str) or not authorization_code:
                    raise CodexOAuthError("Codex device response has no authorization code")
                if not isinstance(code_verifier, str) or not code_verifier:
                    raise CodexOAuthError("Codex device response has no code verifier")
                if on_progress:
                    on_progress("Authorization received; exchanging token…")
                return await exchange_codex_authorization_code(
                    authorization_code,
                    code_verifier,
                    redirect_uri=CODEX_DEVICE_REDIRECT_URI,
                    http=session,
                    timeout=timeout,
                    label=label,
                )
            raise CodexOAuthError("Codex device authorization timed out")
        except CodexOAuthError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise CodexOAuthError(
                "Codex device authorization transport failure",
                retryable=True,
            ) from exc
    finally:
        if owns_session and session is not None:
            await session.close()


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

    endpoint = _oauth_endpoint()
    client_id = _oauth_client_id()
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
                data = await _response_data(response)

                if status < 200 or status >= 300:
                    raise _classify_http_error("refresh", status, data)
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
