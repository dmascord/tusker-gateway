"""Tests for the /v1/responses compatibility endpoint."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.mark.asyncio
async def test_responses_image_input_selects_image_capable_pool_candidate(app, client):
    pool_manager = MagicMock()
    pool_manager.select.return_value = ("minimax", "MiniMax-M3")
    app["pool_manager"] = pool_manager

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "a red square"}}],
        }
        resp = await client.post(
            "/v1/responses",
            json={
                "model": "hermes-code",
                "input": [
                    {"type": "input_text", "text": "What is in this image?"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,AA",
                        "detail": "low",
                    },
                ],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    assert pool_manager.select.call_args.kwargs["required_input_modalities"] == frozenset({"image"})
    messages = mock_chat.call_args.args[2]
    assert messages[0]["content"][1] == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64,AA",
            "detail": "low",
        },
    }


@pytest.mark.asyncio
async def test_responses_endpoint_anchors_fallback_session_to_input_history(client):
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }
        resp = await client.post(
            "/v1/responses",
            json={"model": "opencode-go::minimax-m3", "input": "first turn"},
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200

    session_id = mock_chat.call_args.kwargs["conversation_id"]
    assert session_id.startswith("tusker-")


@pytest.mark.asyncio
async def test_responses_endpoint_preserves_tool_turns_and_request_controls(client):
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "id": "chatcmpl-tool-result",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "The file is readable.",
                    "tool_calls": [{
                        "id": "call_next",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"README.md"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        }

        payload = {
            "model": "openai::gpt-4o",
            "instructions": "Use the read tool when needed.",
            "input": [
                {"type": "message", "role": "user", "content": "inspect README"},
                {
                    "type": "function_call",
                    "id": "fc_read",
                    "call_id": "call_read",
                    "name": "read",
                    "arguments": {"path": "README.md"},
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_read",
                    "output": {"text": "contents"},
                    "status": "failed",
                },
            ],
            "tools": [{
                "type": "function",
                "name": "read",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                "strict": True,
            }],
            "tool_choice": {"type": "function", "name": "read"},
            "max_output_tokens": 99,
        }

        resp = await client.post("/v1/responses", json=payload, headers=HEADERS_AUTH)

        assert resp.status == 200
        data = await resp.json()

    args, kwargs = mock_chat.call_args
    assert args[0:2] == ("openai", "gpt-4o")
    assert args[2] == [
        {"role": "system", "content": "Use the read tool when needed."},
        {"role": "user", "content": "inspect README"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_read",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": '{"path": "README.md"}',
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read",
            "content": {"text": "contents"},
            "is_error": True,
        },
    ]
    assert kwargs["tools"][0]["function"]["strict"] is True
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "read"},
    }
    assert kwargs["extra_body"]["max_tokens"] == 99
    assert "max_output_tokens" not in kwargs["extra_body"]

    assert data["output"][0]["type"] == "message"
    assert data["output"][0]["content"][0]["text"] == "The file is readable."
    function_output = next(item for item in data["output"] if item["type"] == "function_call")
    assert function_output["call_id"] == "call_next"
    assert json.loads(function_output["arguments"]) == {"path": "README.md"}
    assert data["usage"] == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
    }


@pytest.mark.asyncio
async def test_responses_streaming_does_not_return_non_stream_json(client):
    """A Responses stream must use Responses SSE framing, not Chat JSON."""
    async def upstream():
        yield b'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
        yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = upstream()
        resp = await client.post(
            "/v1/responses",
            json={"model": "openai::gpt-4o", "input": "hello", "stream": True},
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        content = await resp.read()

    assert b"event: response.created" in content
    assert b"event: response.output_text.delta" in content
    assert b'"delta": "hello"' in content
    assert b"event: response.completed" in content
    assert b"chat.completion.chunk" not in content


@pytest.mark.asyncio
async def test_responses_streaming_preserves_native_tool_call_deltas(client):
    async def upstream():
        yield (
            b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_read","type":"function","function":{"name":"read","arguments":"{\\"path\\":"}}]},"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"README.md\\"}"}}]},"finish_reason":null}]}\n\n'
        )
        yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        yield b'data: [DONE]\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = upstream()
        resp = await client.post(
            "/v1/responses",
            json={
                "model": "openai::gpt-4o",
                "input": "read README",
                "stream": True,
                "tools": [{"type": "function", "name": "read", "parameters": {"type": "object"}}],
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        content = await resp.read()

    assert b"event: response.output_item.added" in content
    assert b'"type": "function_call"' in content
    assert b'"call_id": "call_read"' in content
    assert b"event: response.function_call_arguments.delta" in content
    assert b"README.md" in content
    assert b"event: response.output_item.done" in content
    assert b"event: response.completed" in content


@pytest.mark.asyncio
async def test_responses_streaming_converts_complete_tool_response(client):
    result = {
        "id": "chatcmpl-complete",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_complete",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path":"README.md"}',
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = result
        resp = await client.post(
            "/v1/responses",
            json={"model": "openai::gpt-4o", "input": "read README", "stream": True},
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        content = await resp.read()

    assert b'"call_id": "call_complete"' in content
    assert b'"arguments": "{\\"path\\":\\"README.md\\"}"' in content
    assert b"event: response.completed" in content


def test_responses_output_preserves_legacy_function_call():
    from tusker_gateway.endpoints import _chat_result_to_responses

    result = {
        "choices": [{
            "message": {
                "role": "assistant",
                "function_call": {
                    "name": "read",
                    "arguments": '{"path":"README.md"}',
                },
            },
            "finish_reason": "function_call",
        }],
    }

    response = _chat_result_to_responses(result, "gpt-4o")

    function_call = next(item for item in response["output"] if item["type"] == "function_call")
    assert function_call["name"] == "read"
    assert json.loads(function_call["arguments"]) == {"path": "README.md"}


def test_responses_output_preserves_all_chat_choices():
    """The compatibility adapter must not collapse an upstream n>1 result."""
    from tusker_gateway.endpoints import _chat_result_to_responses

    response = _chat_result_to_responses({
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "first"},
                "finish_reason": "stop",
            },
            {
                "index": 1,
                "message": {"role": "assistant", "content": "second"},
                "finish_reason": "stop",
            },
        ],
    }, "gpt-4o")

    texts = [
        item["content"][0]["text"]
        for item in response["output"]
        if item["type"] == "message" and item["content"]
    ]
    assert texts == ["first", "second"]


@pytest.mark.asyncio
async def test_chat_rejects_orphaned_tool_message(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "openai::gpt-4o",
            "messages": [{"role": "tool", "content": "orphaned"}],
        },
        headers=HEADERS_AUTH,
    )

    assert resp.status == 400
    data = await resp.json()
    assert data["error"]["code"] == "invalid_messages"
