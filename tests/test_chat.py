"""Tests for chat completions endpoint, including spec validation."""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from .conftest import HEADERS_AUTH, HEADERS_NO_AUTH


@pytest.mark.asyncio
async def test_chat_completions_requires_auth(client):
    resp = await client.post("/v1/chat/completions", json={"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expected_code", [
    ({}, "invalid_request"),
    ({"model": "hermes-code"}, "invalid_request"),
    ({"model": "hermes-code", "messages": []}, "invalid_messages"),
    ({"model": "hermes-code", "messages": [{"role": "user"}]}, "invalid_messages"),
    ({"model": "hermes-code", "messages": [{"role": "bot", "content": "hi"}]}, "invalid_messages"),
    ({"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": "invalid"}, "invalid_stream"),
])
async def test_chat_completions_validation(client, payload, expected_code):
    resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
    assert resp.status == 400
    data = await resp.json()
    assert data["error"]["code"] == expected_code


@pytest.mark.asyncio
async def test_chat_completions_pool_dispatch(client):
    # Mock PassthroughClient.chat
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {"id": "mock-id", "choices": [{"message": {"role": "assistant", "content": "hello"}}]}

        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}]}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        assert resp.status == 200
        data = await resp.json()
        assert data["choices"][0]["message"]["content"] == "hello"

        # Verify provider selection. Default `code` pool contains both
        # openai-codex/{gpt-5.6-luna,gpt-5.4-mini} (kept) and
        # openrouter/openai/gpt-oss-20b:free (kept). When no quality data
        # is recorded, rank() uses an adaptive floor and falls back to
        # the first candidate — which is the openrouter entry because it
        # comes first in the default pool JSON.
        args, kwargs = mock_chat.call_args
        assert args[0] == "openrouter"
        assert args[1] == "openai/gpt-oss-20b:free"


@pytest.mark.asyncio
async def test_chat_completions_streaming(client):
    # Mock streaming response. The endpoint sends [DONE] itself on EOF, so
    # the upstream mock only yields content chunks here.
    async def mock_upstream_stream(*args, **kwargs):
        yield b'data: {"content": "a"}\n\n'
        yield b'data: {"content": "b"}\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = mock_upstream_stream()

        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        # New hardening headers: disables nginx/traefik proxy buffering so
        # heartbeats and incremental chunks reach the client immediately.
        assert resp.headers.get("X-Accel-Buffering") == "no"

        content = await resp.read()
        lines = content.splitlines()
        assert b'data: {"content": "a"}' in lines
        assert b'data: {"content": "b"}' in lines
        assert b'data: [DONE]' in lines


@pytest.mark.asyncio
async def test_chat_completions_streaming_emits_role_chunk_first(client):
    """First bytes must be a parseable role chunk so clients have immediate signal."""
    async def mock_upstream_stream(*args, **kwargs):
        yield b'data: {"content": "hello"}\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = mock_upstream_stream()
        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        content = await resp.read()

    # First SSE event must be a role chunk (per OpenAI streaming reference).
    first_event = content.split(b"\n\n", 1)[0]
    assert first_event.startswith(b"data: ")
    payload_json = json.loads(first_event[len(b"data: "):])
    assert payload_json["choices"][0]["delta"]["role"] == "assistant"


@pytest.mark.asyncio
async def test_chat_completions_streaming_emits_heartbeat_when_upstream_is_slow(
    client, monkeypatch,
):
    """If the upstream pauses between chunks, SSE keepalives must keep flowing.

    This is the regression test for the "socket connection was closed
    unexpectedly" symptom: without periodic comments, idle-connection
    timers on Traefik/Cloudflare/CloudFront would expire and rebalance
    the TCP socket underneath us.

    Note: the aiohttp test client buffers the response before `read()`
    returns, so we count heartbeats in the *final* body — the endpoint's
    heartbeat task writes them as it runs, regardless of when the test
    client decides to flush them downstream.
    """
    # Shrink the heartbeat interval so the test stays fast.
    monkeypatch.setenv("TUSKER_SSE_HEARTBEAT_SECS", "0.05")

    async def slow_upstream(*args, **kwargs):
        # First chunk arrives immediately, then a long silence.
        yield b'data: {"content": "first"}\n\n'
        await asyncio.sleep(0.3)  # 6 heartbeat ticks worth
        yield b'data: {"content": "second"}\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = slow_upstream()
        payload = {
            "model": "hermes-code",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        content = await resp.read()

    # Count SSE comment lines.
    heartbeat_count = content.count(b": keepalive\n")
    assert heartbeat_count >= 2, (
        f"expected at least 2 keepalive comments during the 0.3s upstream pause, "
        f"got {heartbeat_count}; full body:\n{content!r}"
    )
    # And the actual chunks still made it through.
    assert b'data: {"content": "first"}' in content
    assert b'data: {"content": "second"}' in content
    assert b'data: [DONE]' in content


@pytest.mark.asyncio
async def test_chat_completions_streaming_no_heartbeat_when_disabled(client, monkeypatch):
    """TUSKER_SSE_HEARTBEAT_SECS=0 must turn off keepalives entirely."""
    monkeypatch.setenv("TUSKER_SSE_HEARTBEAT_SECS", "0")

    async def fast_upstream(*args, **kwargs):
        yield b'data: {"content": "x"}\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = fast_upstream()
        payload = {
            "model": "hermes-code",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        content = await resp.read()

    # No keepalives should have been emitted.
    assert b": keepalive" not in content
    assert b'data: {"content": "x"}' in content
    assert b'data: [DONE]' in content
