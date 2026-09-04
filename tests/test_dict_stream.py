"""Tests for converting dict responses to streaming chunks (codex path).

When the passthrough layer returns a complete dict (e.g. openai-codex
parses SSE internally), the endpoint must convert it to proper SSE
streaming chunks so clients (OMP) receive content via text deltas.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

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
async def test_streaming_dict_response_preserves_legacy_function_call(client):
    """Complete legacy calls must remain executable on the streaming path."""
    dict_response = {
        "id": "chatcmpl-legacy-1",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "function_call": {
                    "name": "read",
                    "arguments": '{"path":"README.md"}',
                },
            },
            "finish_reason": "function_call",
        }],
    }
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = dict_response
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "hermes-code", "messages": [{"role": "user", "content": "read it"}], "stream": True},
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        content = await resp.read()

    assert b'"name": "read"' in content
    assert b'README.md' in content
    assert b'"finish_reason": "tool_calls"' in content
    assert b"data: [DONE]" in content


@pytest.mark.asyncio
async def test_streaming_legacy_function_call_deltas_are_normalized(client):
    """Legacy streamed function_call fragments must not disappear."""
    async def upstream():
        yield b'data: {"choices":[{"index":0,"delta":{"function_call":{"name":"read","arguments":"{\\"path\\":"}},"finish_reason":null}]}' + b"\n\n"
        yield b'data: {"choices":[{"index":0,"delta":{"function_call":{"arguments":"\\"README.md\\"}"}},"finish_reason":null}]}' + b"\n\n"
        yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"function_call"}]}\n\n'
        yield b"data: [DONE]\n\n"

    tools = [{
        "type": "function",
        "function": {
            "name": "read",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }]
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = upstream()
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "read it"}],
                "tools": tools,
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        content = await resp.read()

    assert b'"name": "read"' in content
    assert b'README.md' in content
    assert b"data: [DONE]" in content


@pytest.mark.asyncio
async def test_streaming_dict_response_preserves_parallel_tool_call_indexes(client):
    """Parallel calls must keep distinct indexes so clients do not merge them."""
    dict_response = {
        "id": "chatcmpl-codex-parallel-1",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-todo",
                        "type": "function",
                        "function": {
                            "name": "todo",
                            "arguments": '{"op":"start","task":"Verify tool indexes"}',
                        },
                    },
                    {
                        "id": "call-read",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"package.json"}',
                        },
                    },
                ],
            },
            "finish_reason": "tool_calls",
        }],
    }
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = dict_response
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "inspect the project"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        content = await resp.read()

    tool_calls = []
    for line in content.splitlines():
        if not line.startswith(b"data: {"):
            continue
        event = json.loads(line[len(b"data: "):])
        for choice in event.get("choices", []):
            tool_calls.extend((choice.get("delta") or {}).get("tool_calls") or [])

    assert [call["index"] for call in tool_calls] == [0, 1]
    assert [call["id"] for call in tool_calls] == ["call-todo", "call-read"]
    assert [call["function"]["name"] for call in tool_calls] == ["todo", "read"]
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {
        "op": "start",
        "task": "Verify tool indexes",
    }
    assert json.loads(tool_calls[1]["function"]["arguments"]) == {"path": "package.json"}
    assert b"data: [DONE]" in content


@pytest.mark.asyncio
async def test_streaming_dict_response_missing_required_tool_args_falls_back(app, client):
    """Codex-style complete responses must not send ``read {}`` to OMP."""
    pool_manager = MagicMock()
    pool_manager.select.side_effect = [
        ("openai-codex", "gpt-5.6-luna-bad"),
        ("openrouter", "tool-capable-fallback"),
    ]
    app["pool_manager"] = pool_manager

    tools = [{
        "type": "function",
        "function": {
            "name": "read",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }]
    invalid_response = {
        "id": "chatcmpl-invalid-tool-args",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-invalid-tool-args",
                    "type": "function",
                    "function": {"name": "read", "arguments": ""},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }
    valid_response = {
        "id": "chatcmpl-valid-tool-args",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-valid-tool-args",
                    "type": "function",
                    "function": {
                        "name": "read",
                        "arguments": '{"path":"/tmp"}',
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.side_effect = [invalid_response, valid_response]
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "read it"}],
                "tools": tools,
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        content = await resp.read()

    assert resp.status == 200
    assert b'"name": "read"' in content
    assert b'\\"path\\":\\"/tmp\\"' in content
    assert mock_chat.call_count == 2
    assert pool_manager.select.call_count == 2


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


def test_build_extra_body_maps_max_completion_tokens():
    """_build_extra_body must map max_completion_tokens -> max_tokens."""
    from tusker_gateway.endpoints import _build_extra_body
    body = {
        "max_completion_tokens": 1000,
    }
    extra = _build_extra_body(body)
    assert extra["max_tokens"] == 1000
    assert "max_completion_tokens" not in extra
