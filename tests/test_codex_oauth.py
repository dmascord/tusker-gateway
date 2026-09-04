"""Tests for provider-specific OAuth refresh and rotation."""
from __future__ import annotations

import json
import base64
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import pytest


class _Response:
    def __init__(self, status: int, body: Any):
        self.status = status
        self._body = body

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def text(self) -> str:
        return json.dumps(self._body)


class _Session:
    def __init__(self, response: _Response):
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        return self.response


class _SequenceSession:
    def __init__(self, responses: list[_Response]):
        self.responses = iter(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        return next(self.responses)


def _jwt(payload: dict[str, Any]) -> str:
    def encode(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}.signature"


def test_codex_authorization_request_uses_pkce_and_fixed_protocol_params():
    from tusker_gateway.codex_oauth import (
        CODEX_DEFAULT_REDIRECT_URI,
        CODEX_OAUTH_CLIENT_ID,
        new_codex_authorization_request,
    )

    url, state, verifier = new_codex_authorization_request()
    query = parse_qs(url.split("?", 1)[1])

    assert query["client_id"] == [CODEX_OAUTH_CLIENT_ID]
    assert query["redirect_uri"] == [CODEX_DEFAULT_REDIRECT_URI]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [state]
    assert len(verifier) >= 43
    assert query["code_challenge"][0]


@pytest.mark.asyncio
async def test_codex_device_issue_exchanges_code_and_returns_native_credential():
    import tusker_gateway.codex_oauth as codex_oauth

    access_token = _jwt(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"},
            "https://api.openai.com/profile": {"email": "User@Example.com"},
            "exp": time.time() + 3600,
        }
    )
    session = _SequenceSession(
        [
            _Response(200, {"device_auth_id": "device-1", "user_code": "ABCD-EFGH", "interval": 0}),
            _Response(403, {"error": "authorization_pending"}),
            _Response(200, {"authorization_code": "auth-code", "code_verifier": "verifier"}),
            _Response(
                200,
                {
                    "access_token": access_token,
                    "refresh_token": "refresh-1",
                    "expires_in": 3600,
                },
            ),
        ]
    )
    authorization: list[tuple[str, str]] = []
    progress: list[str] = []

    credential = await codex_oauth.issue_codex_device_token(
        http=session,
        poll_interval_seconds=0,
        max_polls=3,
        label="primary",
        on_authorize=lambda url, code: authorization.append((url, code)),
        on_progress=progress.append,
    )

    assert authorization == [(codex_oauth.CODEX_DEVICE_AUTH_URL, "ABCD-EFGH")]
    assert progress == ["Waiting for Codex authorization…", "Authorization received; exchanging token…"]
    assert credential["provider"] == "openai-codex"
    assert credential["label"] == "primary"
    assert credential["access_token"] == access_token
    assert credential["refresh_token"] == "refresh-1"
    assert credential["account_id"] == "acct-1"
    assert credential["email"] == "user@example.com"
    assert credential["expires_at_ms"] > int(time.time() * 1000)
    assert session.calls[0][0] == codex_oauth.CODEX_DEVICE_USERCODE_URL
    assert session.calls[0][1]["json"] == {"client_id": codex_oauth.CODEX_OAUTH_CLIENT_ID}
    exchange_payload = parse_qs(session.calls[3][1]["data"].decode())
    assert exchange_payload["grant_type"] == ["authorization_code"]
    assert exchange_payload["code"] == ["auth-code"]
    assert exchange_payload["code_verifier"] == ["verifier"]


def test_codex_headers_accept_namespaced_account_claims():
    from tusker_gateway.auth_strategies import codex_auth_headers

    access_token = _jwt(
        {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-namespaced"}}
    )
    headers = codex_auth_headers(access_token)

    assert headers["Authorization"] == f"Bearer {access_token}"
    assert headers["ChatGPT-Account-ID"] == "acct-namespaced"


@pytest.mark.asyncio
async def test_codex_refresh_uses_openai_oauth_protocol(monkeypatch: pytest.MonkeyPatch):
    from tusker_gateway.codex_oauth import (
        CODEX_OAUTH_CLIENT_ID,
        CODEX_REFRESH_TOKEN_URL,
        refresh_codex_token,
    )

    session = _Session(
        _Response(
            200,
            {
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )
    )
    monkeypatch.delenv("TUSKER_CODEX_REFRESH_TOKEN_URL", raising=False)
    monkeypatch.delenv("CODEX_REFRESH_TOKEN_URL_OVERRIDE", raising=False)
    monkeypatch.delenv("TUSKER_CODEX_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("CODEX_APP_SERVER_LOGIN_CLIENT_ID", raising=False)

    data, expires_at = await refresh_codex_token("refresh-value", http=session)

    assert data["access_token"] == "new-access"
    assert expires_at > time.time()
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == CODEX_REFRESH_TOKEN_URL
    assert kwargs["json"] == {
        "client_id": CODEX_OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": "refresh-value",
    }
    assert kwargs["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_codex_refresh_classifies_rejected_token_without_body_leak():
    from tusker_gateway.codex_oauth import CodexOAuthError, refresh_codex_token

    session = _Session(
        _Response(
            400,
            {"error": "invalid_grant", "description": "refresh token rejected"},
        )
    )

    with pytest.raises(CodexOAuthError) as caught:
        await refresh_codex_token("refresh-value", http=session)

    assert caught.value.status == 400
    assert caught.value.code == "invalid_grant"
    assert not caught.value.retryable
    assert str(caught.value) == "Codex OAuth refresh rejected"
    assert "refresh token rejected" not in str(caught.value)


@pytest.mark.asyncio
async def test_codex_rotator_uses_codex_authority_and_persists_rotation(
    monkeypatch: pytest.MonkeyPatch,
):
    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        return_value=(
            {
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "id_token": "new-id",
            },
            time.time() + 3600,
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)
    credential = {
        "access_token": "expired-access",
        "refresh_token": "old-refresh",
        "expires_at_ms": int((time.time() - 60) * 1000),
    }
    rotator = CodexTokenRotator(
        [credential],
        http_client=object(),
        provider="openai-codex",
    )

    assert await rotator.get_token() == "new-access"
    assert credential["refresh_token"] == "rotated-refresh"
    assert credential["id_token"] == "new-id"
    assert credential["access_token"] == "new-access"
    refresh.assert_awaited_once_with("old-refresh", http=rotator._http)


@pytest.mark.asyncio
async def test_codex_rotation_preserves_other_auth_pools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "openai-codex": [],
                    "copilot": [{"id": "copilot-keep"}],
                    "minimax": [{"id": "minimax-keep"}],
                },
            }
        )
    )
    refresh = AsyncMock(
        return_value=(
            {
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
            },
            time.time() + 3600,
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)
    credential = {
        "access_token": "expired-access",
        "refresh_token": "old-refresh",
        "expires_at_ms": int((time.time() - 60) * 1000),
    }
    rotator = CodexTokenRotator(
        [credential],
        auth_file=str(auth_file),
        http_client=object(),
        provider="openai-codex",
    )

    assert await rotator.get_token() == "new-access"

    saved = json.loads(auth_file.read_text())
    assert saved["credential_pool"]["copilot"] == [{"id": "copilot-keep"}]
    assert saved["credential_pool"]["minimax"] == [{"id": "minimax-keep"}]
    assert saved["credential_pool"]["openai-codex"][0]["refresh_token"] == "rotated-refresh"


def test_auth_cli_metadata_handles_hermes_expiry(tmp_path):
    from tusker_gateway.copilot_enroll import list_credentials

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "openai-codex": [
                        {
                            "label": "codex",
                            "provider": "openai-codex",
                            "base_url": "https://chatgpt.com/backend-api/codex",
                            "access_token": "access",
                            "expires_at_ms": 4_200_000_000_000,
                        }
                    ]
                },
            }
        )
    )

    entry = list_credentials(auth_file)[0]

    assert entry["host"] == "chatgpt.com"
    assert entry["expires_at"] == 4_200_000_000


@pytest.mark.asyncio
async def test_rotator_keeps_copilot_exchange_separate(
    monkeypatch: pytest.MonkeyPatch,
):
    import tusker_gateway.copilot_exchange as copilot_exchange
    from tusker_gateway.passthrough import CodexTokenRotator

    exchange = AsyncMock(return_value=("copilot-access", time.time() + 3600))
    monkeypatch.setattr(copilot_exchange, "exchange_copilot_token", exchange)
    credential = {
        "access_token": "expired-access",
        "refresh_token": "github-refresh",
        "expires_at_ms": int((time.time() - 60) * 1000),
        "host": "github.com",
    }
    rotator = CodexTokenRotator(
        [credential],
        http_client=object(),
        provider="github-copilot",
    )

    assert await rotator.get_token() == "copilot-access"
    exchange.assert_awaited_once_with(
        "github-refresh",
        base_url=None,
        http=rotator._http,
    )


@pytest.mark.asyncio
async def test_rotator_skips_expired_credential_after_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.codex_oauth import CodexOAuthError
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        side_effect=CodexOAuthError(
            "Codex OAuth refresh rejected",
            status=401,
            code="invalid_grant",
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)
    rotator = CodexTokenRotator(
        [
            {
                "access_token": "expired-access",
                "refresh_token": "invalid-refresh",
                "expires_at_ms": int((time.time() - 60) * 1000),
            }
        ],
        http_client=object(),
        provider="openai-codex",
    )
    rotator._persist = MagicMock()  # type: ignore[method-assign]

    assert await rotator.get_token() is None
    refresh.assert_awaited_once()
    rotator._persist.assert_not_called()
