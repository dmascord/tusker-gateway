"""Tests for converting dict responses to streaming chunks (codex path).

When the passthrough layer returns a complete dict (e.g. openai-codex
parses SSE internally), the endpoint must convert it to proper SSE
streaming chunks so clients (OMP) receive content via text deltas.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from .conftest import HEADERS_AUTH


@pytest.mark.asyncio
async def test_streaming_dict_response_with_content(client):
    """A dict response should be emitted as a single SSE content chunk + [DONE]."""
    dict_response = {
        "id": "chatcmpl-codex-123",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello world"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = dict_response
        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        content = await resp.read()

    lines = content.splitlines()
    # First event is the role chunk (emitted before dict conversion).
    first_event = content.split(b"\n\n", 1)[0]
    first_payload = json.loads(first_event[len(b"data: "):])
    assert first_payload["choices"][0]["delta"]["role"] == "assistant"

    # Second event: content chunk with the actual text.
    assert b'"content": "hello world"' in content
    # Third event: finish_reason chunk.
    assert b'"finish_reason": "stop"' in content
    # Final event: [DONE].
    assert b"data: [DONE]" in content


@pytest.mark.asyncio
async def test_streaming_dict_response_with_tool_calls(client):
    """Dict with tool_calls should emit tool call deltas."""
    dict_response = {
        "id": "chatcmpl-codex-456",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "run_code", "arguments": '{"code": "x"}'}}]
            },
            "finish_reason": "tool_calls"
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = dict_response
        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        assert resp.status == 200
        content = await resp.read()

    # Content is empty so no content chunk should appear, but tool_calls chunk must be there.
    assert b'"function": {"name": "run_code"' in content
    assert b'"finish_reason": "tool_calls"' in content
    assert b"data: [DONE]" in content


@pytest.mark.asyncio
async def test_streaming_dict_response_empty_content(client):
    """Dict with empty content should not emit a content chunk but should emit finish_reason."""
    dict_response = {
        "id": "chatcmpl-codex-789",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        "usage": {},
    }
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = dict_response
        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        assert resp.status == 200
        content = await resp.read()

    # No content chunk (empty content skipped), but finish_reason and [DONE] present.
    assert b'"finish_reason": "stop"' in content
    assert b"data: [DONE]" in content


@pytest.mark.asyncio
async def test_streaming_dict_response_long_content(client):
    """Long content in dict should be emitted as a single chunk (not split)."""
    long_text = "A" * 5000
    dict_response = {
        "id": "chatcmpl-codex-long",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": long_text}, "finish_reason": "stop"}],
        "usage": {},
    }
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = dict_response
        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        content = await resp.read()

    assert long_text.encode() in content
    assert b"data: [DONE]" in content


def test_build_extra_body_filters_gateway_fields():
    """_build_extra_body must strip fields handled by the gateway."""
    from tusker_gateway.endpoints import _build_extra_body
    body = {
        "model": "hermes-code",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "tools": [{"type": "function"}],
        "tool_choice": "auto",
        "max_tokens": 16384,
        "temperature": 0.7,
        "top_p": 0.9,
        "stop": ["\n"],
    }
    extra = _build_extra_body(body)
    # Gateway fields must be stripped.
    assert "model" not in extra
    assert "messages" not in extra
    assert "stream" not in extra
    assert "tools" not in extra
    assert "tool_choice" not in extra
    # Hyperparameters must be preserved.
    assert extra["max_tokens"] == 16384
    assert extra["temperature"] == 0.7
    assert extra["top_p"] == 0.9
    assert extra["stop"] == ["\n"]
