"""Tests for provider-specific OAuth refresh and rotation."""
from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
