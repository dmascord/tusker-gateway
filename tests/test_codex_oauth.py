"""Tests for provider-specific OAuth refresh and rotation."""
from __future__ import annotations

import json
import base64
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import pytest

from tusker_gateway import copilot_enroll


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


def test_concurrent_provider_pool_saves_preserve_both_updates(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    original_write = copilot_enroll._write_hermes_doc

    def slow_write(target, document):
        time.sleep(0.02)
        original_write(target, document)

    monkeypatch.setattr(copilot_enroll, "_write_hermes_doc", slow_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                copilot_enroll.save_provider_auth_pool,
                provider,
                [{"provider": provider, "access_token": provider}],
                path,
            )
            for provider in ("openai-codex", "github-copilot")
        ]
        for future in futures:
            future.result()

    document = json.loads(path.read_text())
    assert set(document["credential_pool"]) == {
        "openai-codex",
        "github-copilot",
    }


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
async def test_rotator_waits_for_database_persistence_and_logs_rotation_state(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    import logging

    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        return_value=(
            {"access_token": "new-access", "refresh_token": "new-refresh"},
            time.time() + 3600,
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)
    writes: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def persist(provider: str, expected: dict[str, Any], replacement: dict[str, Any]) -> bool:
        writes.append((provider, expected, replacement))
        return True

    rotator = CodexTokenRotator(
        [{
            "label": "test-account",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at_ms": int((time.time() - 60) * 1000),
        }],
        http_client=object(),
        provider="openai-codex",
        persist_credentials=persist,
    )

    with caplog.at_level(logging.INFO, logger="tusker_gateway.passthrough"):
        assert await rotator.get_token() == "new-access"

    assert len(writes) == 1
    assert writes[0][0] == "openai-codex"
    assert writes[0][1]["refresh_token"] == "old-refresh"
    assert writes[0][2]["refresh_token"] == "new-refresh"
    assert "refresh_token_rotated=True persistence=committed" in caplog.text


@pytest.mark.asyncio
async def test_rotator_reports_missing_rotated_refresh_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    import logging

    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        return_value=({"access_token": "new-access"}, time.time() + 3600)
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)
    writes: list[dict[str, Any]] = []

    def persist(_provider: str, _expected: dict[str, Any], replacement: dict[str, Any]) -> bool:
        writes.append(replacement)
        return True

    rotator = CodexTokenRotator(
        [{
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at_ms": int((time.time() - 60) * 1000),
        }],
        http_client=object(),
        provider="openai-codex",
        persist_credentials=persist,
    )

    with caplog.at_level(logging.INFO, logger="tusker_gateway.passthrough"):
        assert await rotator.get_token() == "new-access"

    assert writes[0]["refresh_token"] == "old-refresh"
    assert "refresh_token_rotated=False persistence=committed" in caplog.text


@pytest.mark.asyncio
async def test_rotator_logs_database_persistence_conflict_without_losing_live_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    import logging

    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        return_value=(
            {"access_token": "new-access", "refresh_token": "new-refresh"},
            time.time() + 3600,
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)
    rotator = CodexTokenRotator(
        [{
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at_ms": int((time.time() - 60) * 1000),
        }],
        http_client=object(),
        provider="openai-codex",
        persist_credentials=lambda *_args: False,
    )

    with caplog.at_level(logging.ERROR, logger="tusker_gateway.passthrough"):
        assert await rotator.get_token() == "new-access"

    assert "outcome=conflict_or_missing" in caplog.text


@pytest.mark.asyncio
async def test_rotator_refreshes_access_token_rejected_before_local_expiry(
    monkeypatch: pytest.MonkeyPatch,
):
    """A provider 401 must force refresh even when the JWT is not expired."""
    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        return_value=(
            {"access_token": "fresh-access", "refresh_token": "fresh-refresh"},
            time.time() + 3600,
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)
    rotator = CodexTokenRotator(
        [{
            "access_token": "revoked-access",
            "refresh_token": "refresh-token",
            "expires_at_ms": 9_999_999_999_999,
        }],
        http_client=object(),
        provider="openai-codex",
    )

    assert await rotator.get_token() == "revoked-access"
    assert await rotator.invalidate_access_token("revoked-access") is True
    assert await rotator.get_token() == "fresh-access"
    refresh.assert_awaited_once_with("refresh-token", http=rotator._http)


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


@pytest.mark.asyncio
async def test_rotator_applies_long_cooldown_for_permanent_refresh_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """refresh_token_reused / invalid_grant skip the 60s retry loop.

    A dead refresh token must not be replayed every 60s forever; that pins a
    single bad credential in front of healthy peers. The rotator must apply
    the permanent-failure cooldown so the bad credential is skipped for the
    long cooldown, letting other credentials (and other providers) carry
    traffic until the operator re-authenticates.
    """
    import os

    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.codex_oauth import CodexOAuthError
    from tusker_gateway.passthrough import CodexTokenRotator

    monkeypatch.delenv(
        "TUSKER_OAUTH_PERMANENT_FAILURE_COOLDOWN_SECS", raising=False
    )

    refresh = AsyncMock(
        side_effect=CodexOAuthError(
            "Codex OAuth refresh rejected",
            status=401,
            code="refresh_token_reused",
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)

    rotator = CodexTokenRotator(
        [
            {
                "access_token": "expired-access",
                "refresh_token": "stale-refresh",
                # Already expired and refused a refresh -> get_token() must
                # skip this credential so the upstream never sees a dead
                # bearer token. The permanent cooldown we want to assert is
                # recorded on the in-memory cooldown map.
                "expires_at_ms": int((time.time() - 60) * 1000),
            }
        ],
        http_client=object(),
        provider="openai-codex",
    )
    rotator._persist = MagicMock()  # type: ignore[method-assign]

    now = time.time()
    assert await rotator.get_token() is None

    # Permanent cooldown (24h) must be applied, not the 60s transient one.
    failed_until = rotator._refresh_failed_until.get(0)
    assert failed_until is not None
    delta = failed_until - now
    # 24h default is 86400s; allow generous slack for test runtime.
    assert delta > 3600, f"expected long cooldown, got {delta}s"
    assert delta >= CodexTokenRotator._DEFAULT_PERMANENT_REFRESH_FAILURE_COOLDOWN_SECONDS - 5
    refresh.assert_awaited_once()
    rotator._persist.assert_not_called()


@pytest.mark.asyncio
async def test_rotator_keeps_short_cooldown_for_transient_refresh_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """Network-class failures must stay on the short (default 60s) cooldown."""
    import os

    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.codex_oauth import CodexOAuthError
    from tusker_gateway.passthrough import CodexTokenRotator

    monkeypatch.delenv(
        "TUSKER_OAUTH_PERMANENT_FAILURE_COOLDOWN_SECS", raising=False
    )

    refresh = AsyncMock(
        side_effect=CodexOAuthError(
            "Codex OAuth refresh rejected",
            status=503,
            code="server_error",
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)

    rotator = CodexTokenRotator(
        [
            {
                "access_token": "expired-access",
                "refresh_token": "transient-refresh",
                # Already expired so get_token() returns None; the transient
                # cooldown is recorded on the in-memory cooldown map.
                "expires_at_ms": int((time.time() - 60) * 1000),
            }
        ],
        http_client=object(),
        provider="openai-codex",
    )
    rotator._persist = MagicMock()  # type: ignore[method-assign]

    now = time.time()
    assert await rotator.get_token() is None

    failed_until = rotator._refresh_failed_until.get(0)
    assert failed_until is not None
    # Transient cooldown (~60s), definitely not 24h.
    delta = failed_until - now
    assert 0 <= delta <= 120, f"expected transient cooldown, got {delta}s"
    refresh.assert_awaited_once()
    rotator._persist.assert_not_called()


def test_rotator_classifies_invalid_grant_as_permanent():
    """invalid_grant must be recognised alongside refresh_token_reused."""
    from tusker_gateway.codex_oauth import CodexOAuthError
    from tusker_gateway.passthrough import CodexTokenRotator

    err = CodexOAuthError(
        "Codex OAuth refresh rejected",
        status=400,
        code="invalid_grant",
    )
    assert CodexTokenRotator._is_permanent_refresh_error(err) is True

    server_err = CodexOAuthError(
        "Codex OAuth refresh rejected",
        status=503,
        code="server_error",
    )
    assert CodexTokenRotator._is_permanent_refresh_error(server_err) is False

@pytest.mark.asyncio
async def test_rotator_with_refresh_disabled_never_calls_oauth_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only rotators must not consume shared single-use refresh tokens.

    Standalone qualification jobs build rotators with ``refresh_enabled=False``
    so they cannot kill the credential pool even when no persistence target
    is wired.
    """
    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        side_effect=AssertionError("refresh must not be called for a read-only rotator")
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)

    rotator = CodexTokenRotator(
        [{
            "access_token": "valid-access",
            "refresh_token": "shared-refresh",
            "expires_at_ms": int((time.time() + 3600) * 1000),
        }],
        http_client=object(),
        provider="openai-codex",
        refresh_enabled=False,
    )

    token = await rotator.get_token()
    assert token == "valid-access"
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotator_persists_to_both_db_callback_and_auth_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful refresh writes to the DB AND to the Hermes auth.json.

    The DB persist is CAS; the file persist is merge-preserving so other
    providers keep their existing entries.
    """
    import logging

    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        return_value=(
            {"access_token": "new-access", "refresh_token": "new-refresh"},
            time.time() + 3600,
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "openai-codex": [],
                    "copilot": [{"id": "copilot-keep"}],
                },
            }
        )
    )
    db_writes: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def persist(provider: str, expected: dict[str, Any], replacement: dict[str, Any]) -> bool:
        db_writes.append((provider, expected, replacement))
        return True

    rotator = CodexTokenRotator(
        [{
            "label": "dual-account",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at_ms": int((time.time() - 60) * 1000),
        }],
        auth_file=str(auth_file),
        http_client=object(),
        provider="openai-codex",
        persist_credentials=persist,
    )

    with caplog.at_level(logging.INFO, logger="tusker_gateway.passthrough"):
        token = await rotator.get_token()

    assert token == "new-access"
    assert len(db_writes) == 1
    saved = json.loads(auth_file.read_text())
    assert saved["credential_pool"]["copilot"] == [{"id": "copilot-keep"}]
    assert saved["credential_pool"]["openai-codex"][0]["refresh_token"] == "new-refresh"
    assert "targets=" in caplog.text
    assert "refresh_token_rotated=True" in caplog.text


@pytest.mark.asyncio
async def test_rotator_adopts_fresher_credential_from_store_before_refresh(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Adopt a credential another pod already rotated instead of refreshing.

    Without adoption, two pods rolling through OAuth refresh would each
    consume the shared refresh token, leaving the second with a 401 and
    ``refresh_token_reused``.
    """
    import logging

    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        side_effect=AssertionError(
            "refresh must not run when store already holds a newer credential"
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)

    in_memory_cred = {
        "account_id": "acct-A",
        "access_token": "expired-access",
        "refresh_token": "old-refresh",
        "expires_at_ms": int((time.time() - 60) * 1000),
    }
    stored_pool = [
        {
            "account_id": "acct-A",
            "access_token": "fresh-access",
            "refresh_token": "rotated-refresh",
            "expires_at_ms": int((time.time() + 3600) * 1000),
        }
    ]

    def load(_provider: str) -> list[dict[str, Any]]:
        return stored_pool

    rotator = CodexTokenRotator(
        [in_memory_cred],
        http_client=object(),
        provider="openai-codex",
        load_credentials=load,
    )

    with caplog.at_level(logging.INFO, logger="tusker_gateway.passthrough"):
        token = await rotator.get_token()

    assert token == "fresh-access"
    refresh.assert_not_awaited()
    assert "oauth credential adopted from store" in caplog.text
    assert rotator._creds[0]["refresh_token"] == "rotated-refresh"


@pytest.mark.asyncio
async def test_rotator_keeps_team_seats_distinct_during_store_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seats of one team account must not collapse into a single credential.

    A ChatGPT *team* gives every member the same ``account_id``. Matching
    adoption candidates on that field made the pool adopt seat 1's rotated
    tokens into seats 2 and 3, leaving three copies of one access token in
    the rotator. One quota 429 then cooled all three by token fingerprint
    and the pool reported ``pool_empty_or_all_cooling`` (2026-10-31,
    ``openai-codex/gpt-5.6-luna``).
    """
    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        side_effect=AssertionError(
            "refresh must not run: every seat already holds a fresh token"
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)

    team_account_id = "80b149a8-0bfa-430f-aa41-7e6818308e81"
    expired_at_ms = int((time.time() - 60) * 1000)
    expires_at_ms = int((time.time() + 3600) * 1000)
    in_memory = [
        {
            "label": f"codex-{index}",
            "account_id": team_account_id,
            "account_user_id": f"user-{index}",
            "email": f"seat-{index}@example.test",
            "access_token": f"access-{index}",
            "refresh_token": f"refresh-{index}",
            "expires_at_ms": expired_at_ms,
        }
        for index in (1, 2, 3)
    ]
    stored = [
        {
            **credential,
            "access_token": f"rotated-access-{index}",
            "refresh_token": f"rotated-refresh-{index}",
            "expires_at_ms": expires_at_ms,
        }
        for index, credential in enumerate(in_memory, start=1)
    ]

    rotator = CodexTokenRotator(
        in_memory,
        http_client=object(),
        provider="openai-codex",
        load_credentials=lambda _provider: [dict(entry) for entry in stored],
    )

    selected = [await rotator.get_token() for _ in range(3)]

    assert selected == ["rotated-access-1", "rotated-access-2", "rotated-access-3"]
    assert len({rotator.fingerprint(token) for token in selected}) == 3
    # Each seat keeps its own label, so operator logs stay unambiguous.
    assert [rotator._credential_label(cred, index) for index, cred in enumerate(rotator._creds)] == [
        "codex-1",
        "codex-2",
        "codex-3",
    ]
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotator_model_exclusion_is_scoped_to_one_team_seat() -> None:
    """An entitlement failure must not exclude the model for the whole team.

    ``account_id`` identifies the team, not the seat. Recording a per-seat
    entitlement failure against it excluded the model for every member of
    the team.
    """
    from tusker_gateway.passthrough import CodexTokenRotator

    team_account_id = "80b149a8-0bfa-430f-aa41-7e6818308e81"
    rotator = CodexTokenRotator([
        {
            "label": "codex-1",
            "account_id": team_account_id,
            "account_user_id": "user-1",
            "email": "seat-1@example.test",
            "access_token": "access-1",
        },
        {
            "label": "codex-2",
            "account_id": team_account_id,
            "account_user_id": "user-2",
            "email": "seat-2@example.test",
            "access_token": "access-2",
        },
    ])

    assert rotator.exclude_model_for_token("access-1", "gpt-5.4-mini")
    assert not rotator.model_excluded_for_all_credentials("gpt-5.4-mini")
    assert await rotator.get_token(model="gpt-5.4-mini") == "access-2"


@pytest.mark.asyncio
async def test_rotator_initialised_log_redacts_tokens(caplog: pytest.LogCaptureFixture) -> None:
    """Initial rotator state must show fingerprints, never raw tokens."""
    import logging

    from tusker_gateway.passthrough import CodexTokenRotator

    # Construction is inside the capture scope: the initialised log line is
    # emitted from __init__, before any request touches the rotator.
    with caplog.at_level(logging.INFO, logger="tusker_gateway.passthrough"):
        rotator = CodexTokenRotator(
            [
                {
                    "access_token": "sensitive-access",
                    "refresh_token": "sensitive-refresh",
                    "expires_at_ms": int((time.time() + 3600) * 1000),
                },
                {
                    "access_token": "other-access",
                    "refresh_token": "other-refresh",
                    "expires_at_ms": int((time.time() + 3600) * 1000),
                },
            ],
            http_client=object(),
            provider="openai-codex",
        )
        await rotator.get_token()

    init_log = [
        record.message
        for record in caplog.records
        if "oauth rotator initialized" in record.message
    ]
    assert init_log, "expected an initialised log line"
    line = init_log[0]
    assert "sensitive-access" not in line
    assert "sensitive-refresh" not in line
    assert "other-access" not in line
    assert "refresh_fps=" in line
    assert "access_fps=" in line


@pytest.mark.asyncio
async def test_rotator_adopts_store_credential_while_refresh_cooldown_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permanent refresh failure must not block store adoption.

    When this process burns its copy of a shared refresh token it records a
    24h permanent cooldown. Another process may still rotate that seat and
    persist the result, so the cooldown must gate *refreshing* only: the
    rotator has to keep reading the store, otherwise the pod stays dead for
    24h holding a credential that is valid in the database.
    """
    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.codex_oauth import CodexOAuthError
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        side_effect=CodexOAuthError(
            "Codex OAuth refresh rejected",
            status=401,
            code="refresh_token_reused",
        )
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)

    seat = {
        "label": "codex-1",
        "account_user_id": "user-1",
        "access_token": "expired-access",
        "refresh_token": "consumed-refresh",
        "expires_at_ms": int((time.time() - 60) * 1000),
    }
    rotated_by_peer = {
        **seat,
        "access_token": "peer-access",
        "refresh_token": "peer-refresh",
        "expires_at_ms": int((time.time() + 3600) * 1000),
    }
    store: list[dict[str, Any]] = []

    rotator = CodexTokenRotator(
        [dict(seat)],
        http_client=object(),
        provider="openai-codex",
        load_credentials=lambda _provider: [dict(entry) for entry in store],
    )
    rotator._persist = MagicMock()  # type: ignore[method-assign]

    # First pass: no peer rotation yet, so the refresh runs and is rejected
    # permanently, arming the 24h cooldown.
    assert await rotator.get_token() is None
    assert rotator._refresh_failed_until.get(0, 0.0) > time.time() + 3600

    # Peer rotates the same seat and persists it.
    store.append(rotated_by_peer)

    # The permanent cooldown must not stop the store read.
    assert await rotator.get_token() == "peer-access"
    # Adoption, not a second refresh, produced the token.
    refresh.assert_awaited_once()
    assert rotator._creds[0]["refresh_token"] == "peer-refresh"
    # A successful adoption clears the cooldown so the seat is fully live.
    assert 0 not in rotator._refresh_failed_until


def test_has_available_credential_allows_adoption_during_refresh_cooldown():
    """A cooled-down expired seat stays selectable while a store is wired.

    ``has_available_credential`` gates the request path before ``get_token``
    runs. Reporting False for a seat that the store can still refresh would
    short-circuit the only recovery path.
    """
    from tusker_gateway.passthrough import CodexTokenRotator

    stale = {
        "label": "codex-1",
        "account_user_id": "user-1",
        "access_token": "expired-access",
        "refresh_token": "consumed-refresh",
        "expires_at_ms": int((time.time() - 60) * 1000),
    }

    with_store = CodexTokenRotator(
        [dict(stale)],
        http_client=object(),
        provider="openai-codex",
        load_credentials=lambda _provider: [],
    )
    with_store._refresh_failed_until[0] = time.time() + 86400
    assert with_store.has_available_credential("gpt-5.6-luna") is True

    without_store = CodexTokenRotator(
        [dict(stale)],
        http_client=object(),
        provider="openai-codex",
    )
    without_store._refresh_failed_until[0] = time.time() + 86400
    assert without_store.has_available_credential("gpt-5.6-luna") is False


@pytest.mark.asyncio
async def test_canary_rotator_adopts_expired_seat_from_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canary policy forbids rotating, not reading: adopt and serve.

    The canary pod must never rotate a refresh token production also holds,
    but production rotates it regularly and persists the result. An expired
    canary copy that refused to read the store stayed dead with the live
    credential sitting in the database.
    """
    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        side_effect=AssertionError("canary must never rotate a shared refresh token")
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)
    monkeypatch.setenv("TUSKER_CONFIG_CANARY", "true")

    stale = {
        "label": "codex-1",
        "account_user_id": "user-1",
        "access_token": "expired-access",
        "refresh_token": "shared-refresh",
        "expires_at_ms": int((time.time() - 60) * 1000),
    }
    rotated = {
        **stale,
        "access_token": "prod-access",
        "refresh_token": "prod-refresh",
        "expires_at_ms": int((time.time() + 3600) * 1000),
    }

    rotator = CodexTokenRotator(
        [dict(stale)],
        http_client=object(),
        provider="openai-codex",
        load_credentials=lambda _provider: [dict(rotated)],
    )

    assert await rotator.get_token() == "prod-access"
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_canary_never_rotates_a_token_it_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canary that adopted a token must not later rotate that same token.

    The skip set is re-baselined whenever credentials reload from the store.
    Without adding an adopted refresh token to it, a subsequent reload plus
    expiry would let the canary rotate a token the other holder still owns,
    invalidating it there (the seat-collapse failure class).
    """
    import tusker_gateway.codex_oauth as codex_oauth
    from tusker_gateway.passthrough import CodexTokenRotator

    refresh = AsyncMock(
        side_effect=AssertionError("canary must never rotate a shared refresh token")
    )
    monkeypatch.setattr(codex_oauth, "refresh_codex_token", refresh)
    monkeypatch.setenv("TUSKER_CONFIG_CANARY", "true")

    stale = {
        "label": "codex-1",
        "account_user_id": "user-1",
        "access_token": "expired-access",
        "refresh_token": "shared-refresh",
        "expires_at_ms": int((time.time() - 60) * 1000),
    }
    rotated = {
        **stale,
        "access_token": "prod-access",
        "refresh_token": "prod-refresh",
        "expires_at_ms": int((time.time() + 3600) * 1000),
    }

    rotator = CodexTokenRotator(
        [dict(stale)],
        http_client=object(),
        provider="openai-codex",
        load_credentials=lambda _provider: [dict(rotated)],
    )

    assert await rotator.get_token() == "prod-access"
    assert "prod-refresh" in rotator._initial_refresh_tokens

    # Reload from the store keeps the adopted token in the skip set, so the
    # entry is skipped once its access token dies rather than rotated.
    rotator.reload([dict(rotated)])
    assert "prod-refresh" in rotator._initial_refresh_tokens
    assert rotator._canary_skip_rotation(rotator._creds[0]) is True
    refresh.assert_not_awaited()
