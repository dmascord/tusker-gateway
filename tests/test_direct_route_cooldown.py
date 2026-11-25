"""Regression tests for the direct-route cooldown gate.

An explicitly named route (``provider/model``) bypasses pool selection, so it
also bypassed the selector's cooldown filter. A route the gateway already knew
was quarantined was therefore still dispatched upstream and the resulting 429
was recorded as another failure against the same dead route.

The composite image+tools qualification runner had no quarantine filter at
all, so it re-probed cooled routes on every cycle.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from tusker_gateway.cooldown import CooldownTracker, global_tracker
from tusker_gateway.endpoints import _is_loopback_qualification_probe

from .conftest import HEADERS_AUTH


def _request(headers=None, remote="127.0.0.1"):
    class _Request:
        def __init__(self):
            self.headers = headers or {}
            self.remote = remote

    return _Request()


@pytest.mark.asyncio
async def test_direct_route_in_cooldown_does_not_reach_upstream(app, client):
    """A cooled explicit route must fail locally instead of calling upstream."""
    from tusker_gateway.cooldown import global_tracker as tracker_factory

    tracker_factory().cooldown("zai", "glm-5.3-flash", 3600)

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "zai/glm-5.3-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=HEADERS_AUTH,
        )

    assert response.status == 429
    mock_chat.assert_not_awaited()
    payload = await response.json()
    assert payload["error"]["code"] == "rate_limit_exceeded"
    # The client is told the real window so it does not retry into the
    # quarantined route seconds later.
    assert int(response.headers["Retry-After"]) > 3000


@pytest.mark.asyncio
async def test_direct_route_outside_cooldown_still_dispatches(app, client):
    """The gate must not block a route that is eligible."""
    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "zai/glm-5.3-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=HEADERS_AUTH,
        )

    assert response.status == 200
    mock_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_route_respects_provider_wide_cooldown(app, client):
    """A provider-wide cooldown must also gate its explicit routes."""
    tracker = global_tracker()
    tracker.cooldown("zai", "", 1800)

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "zai/glm-4.7",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=HEADERS_AUTH,
        )

    assert response.status == 429
    mock_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_gce_direct_route_in_cooldown_keeps_503_and_window(app, client):
    """Enterprise Copilot is a provider-wide 429 provider.

    The local cooldown gate must reuse the 503 surface (not a client 429)
    and carry the enforced window in Retry-After rather than a fixed 5s
    that would invite an immediate retry into a quarantined route.
    """
    global_tracker().cooldown("github-copilot-enterprise", "gpt-5.6-luna", 7200)

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "github-copilot-enterprise/gpt-5.6-luna",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=HEADERS_AUTH,
        )

    assert response.status == 503
    assert int(response.headers["Retry-After"]) > 3000
    mock_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_cooldown_does_not_block_direct_route(app, client):
    """An elapsed cooldown must not keep the route blocked."""
    tracker = global_tracker()
    tracker.cooldown("zai", "glm-5.3-flash", 3600)
    # Push the windows into the past: the tracker measures against monotonic
    # time, so an elapsed cooldown must not keep the route blocked. A
    # non-model-scoped provider also carries a provider-wide window.
    elapsed = time.monotonic() - 1.0
    tracker._cooldowns[("zai", "glm-5.3-flash")].until = elapsed
    tracker._provider_default["zai"].until = elapsed

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "zai/glm-5.3-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=HEADERS_AUTH,
        )

    assert response.status == 200
    mock_chat.assert_awaited_once()


def test_remaining_cooldown_seconds_reports_longest_window():
    tracker = CooldownTracker()
    tracker.cooldown("zai", "glm-5.3-flash", 3600)
    tracker.cooldown("zai", "", 7200)

    remaining = tracker.remaining_cooldown_seconds("zai", "glm-5.3-flash")
    assert remaining > 7000


def test_remaining_cooldown_seconds_is_zero_for_clear_route():
    tracker = CooldownTracker()
    assert tracker.remaining_cooldown_seconds("zai", "glm-5.3-flash") == 0.0


def test_loopback_qualification_probe_is_exempt():
    for header in (
        "X-Tusker-Tool-Qualification",
        "X-Tusker-Modality-Qualification",
        "X-Tusker-Structured-Qualification",
    ):
        assert _is_loopback_qualification_probe(_request({header: "v1"})) is True


def test_external_qualification_header_is_not_exempt():
    """A remote client must not be able to bypass the gate with the header."""
    request = _request({"X-Tusker-Tool-Qualification": "v1"}, remote="10.0.0.5")
    assert _is_loopback_qualification_probe(request) is False


def test_request_without_qualification_header_is_not_exempt():
    assert _is_loopback_qualification_probe(_request({})) is False
