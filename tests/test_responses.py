"""Tests for the /v1/responses compatibility endpoint."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from .conftest import HEADERS_AUTH


@pytest.mark.asyncio
async def test_responses_endpoint_string_input(client):
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "translated hello"}}]
        }
        
        payload = {"model": "hermes-code", "input": "hi"}
        resp = await client.post("/v1/responses", json=payload, headers=HEADERS_AUTH)
        assert resp.status == 200
        data = await resp.json()
        assert data["object"] == "response"
        assert data["output"][0]["content"][0]["text"] == "translated hello"
        
        # Verify it translated "input" to "messages"
        args, kwargs = mock_chat.call_args
        assert args[2] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_responses_endpoint_array_input(client):
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "response"}}]
        }
        
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]
        payload = {"model": "hermes-code", "input": messages}
        resp = await client.post("/v1/responses", json=payload, headers=HEADERS_AUTH)
        assert resp.status == 200
        
        args, kwargs = mock_chat.call_args
        assert args[2] == messages
