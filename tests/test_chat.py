"""Tests for chat completions endpoint, including spec validation."""
from __future__ import annotations

import json
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
        
        # Verify provider selection (default pool: code -> github-copilot)
        args, kwargs = mock_chat.call_args
        assert args[0] == "github-copilot"


@pytest.mark.asyncio
async def test_chat_completions_streaming(client):
    # Mock streaming response
    async def mock_upstream_stream(*args, **kwargs):
        yield b'data: {"content": "a"}\n\n'
        yield b'data: {"content": "b"}\n\n'
        yield b'data: [DONE]\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = mock_upstream_stream()
        
        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        
        content = await resp.read()
        lines = content.splitlines()
        assert b'data: {"content": "a"}' in lines
        assert b'data: {"content": "b"}' in lines
        assert b'data: [DONE]' in lines
