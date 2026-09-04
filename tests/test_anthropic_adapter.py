"""Tests for the Anthropic Messages API adapter."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from .conftest import HEADERS_AUTH, HEADERS_NO_AUTH


# ---------------------------------------------------------------------------
# anthropic_to_openai conversion tests
# ---------------------------------------------------------------------------

class TestAnthropicToOpenAI:
    def test_basic_conversion(self):
        from tusker_gateway.anthropic_adapter import anthropic_to_openai

        body = {
            "model": "claude-3-opus-20240229",
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": "Hello"},
            ],
        }
        result = anthropic_to_openai(body)
        assert result["model"] == "claude-3-opus-20240229"
        assert result["max_tokens"] == 1024
        assert result["messages"] == [{"role": "user", "content": "Hello"}]

    def test_system_string(self):
        from tusker_gateway.anthropic_adapter import anthropic_to_openai

        body = {
            "model": "claude-3",
            "max_tokens": 100,
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_to_openai(body)
        assert result["messages"][0] == {"role": "system", "content": "You are helpful."}
        assert result["messages"][1] == {"role": "user", "content": "Hi"}

    def test_system_list_of_blocks(self):
        from tusker_gateway.anthropic_adapter import anthropic_to_openai

        body = {
            "model": "claude-3",
            "max_tokens": 100,
            "system": [
                {"type": "text", "text": "Part A"},
                {"type": "text", "text": "Part B"},
            ],
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_to_openai(body)
        assert result["messages"][0] == {"role": "system", "content": "Part A\nPart B"}

    def test_list_content_blocks(self):
        from tusker_gateway.anthropic_adapter import anthropic_to_openai

        body = {
            "model": "claude-3",
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "World"},
                ]},
            ],
        }
        result = anthropic_to_openai(body)
        assert result["messages"][0]["content"] == "Hello\nWorld"

    def test_parameter_mapping(self):
        from tusker_gateway.anthropic_adapter import anthropic_to_openai

        body = {
            "model": "claude-3",
            "max_tokens": 512,
            "temperature": 0.7,
            "top_p": 0.9,
            "stop_sequences": ["STOP"],
            "stream": True,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_to_openai(body)
        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9
        assert result["stop"] == ["STOP"]
        assert result["stream"] is True

    def test_tool_conversion(self):
        from tusker_gateway.anthropic_adapter import anthropic_to_openai

        body = {
            "model": "claude-3",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get weather info",
                    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                }
            ],
        }
        result = anthropic_to_openai(body)
        assert len(result["tools"]) == 1
        assert result["tools"][0]["type"] == "function"
        assert result["tools"][0]["function"]["name"] == "get_weather"
        assert result["tools"][0]["function"]["parameters"]["properties"]["city"]["type"] == "string"

    def test_null_content_in_assistant_message(self):
        from tusker_gateway.anthropic_adapter import anthropic_to_openai

        body = {
            "model": "claude-3",
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": None},
            ],
        }
        result = anthropic_to_openai(body)
        assert result["messages"][1]["content"] == ""

    def test_image_source_normalizes_to_openai_image_url(self):
        from tusker_gateway.anthropic_adapter import anthropic_to_openai

        result = anthropic_to_openai({
            "model": "hermes-code",
            "max_tokens": 100,
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "image",
                    "source": {"type": "url", "url": "https://example.test/image.png"},
                }],
            }],
        })

        assert result["messages"][0]["content"] == [{
            "type": "image_url",
            "image_url": {"url": "https://example.test/image.png"},
        }]


# ---------------------------------------------------------------------------
# openai_to_anthropic conversion tests
# ---------------------------------------------------------------------------

class TestOpenAIToAnthropic:
    def test_basic_conversion(self):
        from tusker_gateway.anthropic_adapter import openai_to_anthropic

        openai_result = {
            "id": "chatcmpl-abc",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_to_anthropic(openai_result, "claude-3-opus-20240229")
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["model"] == "claude-3-opus-20240229"
        assert result["content"] == [{"type": "text", "text": "Hello!"}]
        assert result["stop_reason"] == "end_turn"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5
        assert result["id"].startswith("msg_")

    def test_finish_reason_length(self):
        from tusker_gateway.anthropic_adapter import openai_to_anthropic

        openai_result = {
            "choices": [{"message": {"content": "truncated"}, "finish_reason": "length"}],
            "usage": {},
        }
        result = openai_to_anthropic(openai_result, "claude-3")
        assert result["stop_reason"] == "max_tokens"

    def test_finish_reason_tool_calls(self):
        from tusker_gateway.anthropic_adapter import openai_to_anthropic

        openai_result = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "SF"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        }
        result = openai_to_anthropic(openai_result, "claude-3")
        assert result["stop_reason"] == "tool_use"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "tool_use"
        assert result["content"][0]["name"] == "get_weather"
        assert result["content"][0]["input"] == {"city": "SF"}
        assert result["content"][0]["id"].startswith("toolu_")

    def test_empty_content(self):
        from tusker_gateway.anthropic_adapter import openai_to_anthropic

        openai_result = {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
            "usage": {},
        }
        result = openai_to_anthropic(openai_result, "claude-3")
        assert result["content"] == []


# ---------------------------------------------------------------------------
# AnthropicSSEStreamTranslator tests
# ---------------------------------------------------------------------------

class TestAnthropicSSEStreamTranslator:
    async def _make_stream(self, chunks: list[bytes]):
        async def gen():
            for c in chunks:
                yield c
        return gen()

    async def test_full_stream_lifecycle(self):
        from tusker_gateway.anthropic_adapter import AnthropicSSEStreamTranslator

        openai_chunks = [
            b'data: {"choices": [{"delta": {"content": "Hello"}, "finish_reason": null}]}\n\n',
            b'data: {"choices": [{"delta": {"content": " World"}, "finish_reason": null}]}\n\n',
            b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        stream = await self._make_stream(openai_chunks)
        translator = AnthropicSSEStreamTranslator(stream, model="claude-3", input_tokens=10)

        events = []
        async for chunk in translator:
            events.append(chunk)

        # The translator now concatenates message_start + content_block_start
        # + first text delta into a single emit (rather than emitting one
        # Anthropic event per upstream chunk). This matches the new registry
        # chunk-translator contract.
        assert len(events) == 3

        # First event: message_start + content_block_start + Hello text_delta
        assert b"event: message_start" in events[0]
        assert b"event: content_block_start" in events[0]
        assert b'"text_delta"' in events[0]
        assert b'"Hello"' in events[0]

        # Second event: text_delta " World"
        assert b'"text_delta"' in events[1]
        assert b" World" in events[1]

        # Third event: closing sequence (content_block_stop, message_delta, message_stop)
        closing = events[2]
        assert b"event: content_block_stop" in closing
        assert b"event: message_delta" in closing
        assert b'"stop_reason": "end_turn"' in closing
        assert b"event: message_stop" in closing

    async def test_done_event_triggers_closing(self):
        from tusker_gateway.anthropic_adapter import AnthropicSSEStreamTranslator

        openai_chunks = [
            b'data: {"choices": [{"delta": {"content": "Hi"}, "finish_reason": null}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        stream = await self._make_stream(openai_chunks)
        translator = AnthropicSSEStreamTranslator(stream, model="claude-3")

        events = []
        async for chunk in translator:
            events.append(chunk)

        # Should have: combined message_start + content_block_start + Hi text_delta,
        # then closing sequence.
        assert len(events) == 2
        assert b"event: message_start" in events[0]
        assert b'"Hi"' in events[0]
        assert b"event: message_stop" in events[1]

    async def test_no_content_chunks(self):
        from tusker_gateway.anthropic_adapter import AnthropicSSEStreamTranslator

        openai_chunks = [
            b'data: {"choices": [{"delta": {"role": "assistant"}, "finish_reason": null}]}\n\n',
            b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        stream = await self._make_stream(openai_chunks)
        translator = AnthropicSSEStreamTranslator(stream, model="claude-3")

        events = []
        async for chunk in translator:
            events.append(chunk)

        # message_start + content_block_start (combined), then closing
        # (no text_delta since no content was sent).
        assert len(events) == 2
        assert b"event: message_start" in events[0]
        assert b"event: content_block_start" in events[0]
        assert b"event: message_stop" in events[1]

    async def test_non_data_sse_frame_does_not_close_stream(self):
        """Provider comments/unknown SSE frames must not truncate later text."""
        from tusker_gateway.anthropic_adapter import AnthropicSSEStreamTranslator

        openai_chunks = [
            b": keepalive\n\n",
            b'event: provider.notice\ndata: not-json\n\n',
            b'data: {"choices": [{"delta": {"content": "after"}, "finish_reason": null}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        stream = await self._make_stream(openai_chunks)
        translator = AnthropicSSEStreamTranslator(stream, model="claude-3")

        events = []
        async for chunk in translator:
            events.append(chunk)

        combined = b"".join(events)
        assert b'"after"' in combined
        assert b"event: message_stop" in combined


# ---------------------------------------------------------------------------
# _validate_anthropic_body tests
# ---------------------------------------------------------------------------

class TestValidateAnthropicBody:
    def test_valid_body(self):
        from tusker_gateway.anthropic_adapter import _validate_anthropic_body

        body = {
            "model": "claude-3",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = _validate_anthropic_body(body)
        assert result is body

    def test_missing_model(self):
        from tusker_gateway.anthropic_adapter import _validate_anthropic_body
        from tusker_gateway.errors import BadRequestError

        with pytest.raises(BadRequestError) as exc_info:
            _validate_anthropic_body({"max_tokens": 100, "messages": [{"role": "user", "content": "Hi"}]})
        assert "model is required" in str(exc_info.value.message)

    def test_missing_messages(self):
        from tusker_gateway.anthropic_adapter import _validate_anthropic_body
        from tusker_gateway.errors import BadRequestError

        with pytest.raises(BadRequestError) as exc_info:
            _validate_anthropic_body({"model": "claude-3", "max_tokens": 100})
        assert "messages is required" in str(exc_info.value.message)

    def test_missing_max_tokens(self):
        from tusker_gateway.anthropic_adapter import _validate_anthropic_body
        from tusker_gateway.errors import BadRequestError

        with pytest.raises(BadRequestError) as exc_info:
            _validate_anthropic_body({"model": "claude-3", "messages": [{"role": "user", "content": "Hi"}]})
        assert "max_tokens is required" in str(exc_info.value.message)

    def test_empty_messages(self):
        from tusker_gateway.anthropic_adapter import _validate_anthropic_body
        from tusker_gateway.errors import BadRequestError

        with pytest.raises(BadRequestError):
            _validate_anthropic_body({"model": "claude-3", "max_tokens": 100, "messages": []})

    def test_invalid_role(self):
        from tusker_gateway.anthropic_adapter import _validate_anthropic_body
        from tusker_gateway.errors import BadRequestError

        with pytest.raises(BadRequestError) as exc_info:
            _validate_anthropic_body({
                "model": "claude-3",
                "max_tokens": 100,
                "messages": [{"role": "system", "content": "Hi"}],
            })
        assert "role must be 'user' or 'assistant'" in str(exc_info.value.message)

    def test_non_dict_body(self):
        from tusker_gateway.anthropic_adapter import _validate_anthropic_body
        from tusker_gateway.errors import BadRequestError

        with pytest.raises(BadRequestError):
            _validate_anthropic_body("not a dict")


# ---------------------------------------------------------------------------
# _anthropic_error tests
# ---------------------------------------------------------------------------

class TestAnthropicError:
    def test_error_shape(self):
        from tusker_gateway.anthropic_adapter import _anthropic_error

        err = _anthropic_error("something broke", type="api_error")
        assert err == {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": "something broke",
            },
        }


# ---------------------------------------------------------------------------
# HTTP handler integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_messages_requires_auth(client):
    resp = await client.post("/v1/messages", json={
        "model": "claude-3",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Hi"}],
    })
    assert resp.status == 401


@pytest.mark.asyncio
async def test_messages_validation(client):
    resp = await client.post("/v1/messages", json={}, headers=HEADERS_AUTH)
    assert resp.status == 400
    data = await resp.json()
    assert data["type"] == "error"
    assert data["error"]["type"] == "invalid_request_error"
    assert "model is required" in data["error"]["message"]


@pytest.mark.asyncio
async def test_messages_missing_max_tokens(client):
    resp = await client.post("/v1/messages", json={
        "model": "claude-3",
        "messages": [{"role": "user", "content": "Hi"}],
    }, headers=HEADERS_AUTH)
    assert resp.status == 400
    data = await resp.json()
    assert "max_tokens is required" in data["error"]["message"]


@pytest.mark.asyncio
async def test_messages_non_streaming(client):
    """Non-streaming request converts OpenAI → Anthropic response format."""
    openai_response = {
        "id": "chatcmpl-test",
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello there!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = openai_response
        resp = await client.post("/v1/messages", json={
            "model": "claude-3-opus-20240229",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers=HEADERS_AUTH)
        assert resp.status == 200
        data = await resp.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert data["content"] == [{"type": "text", "text": "Hello there!"}]
        assert data["stop_reason"] == "end_turn"
        assert data["model"] == "claude-3-opus-20240229"
        assert data["usage"]["input_tokens"] == 10
        assert data["usage"]["output_tokens"] == 5


@pytest.mark.asyncio
async def test_messages_streaming_converts_complete_provider_response(client):
    """A parsed Codex-style response must still reach Anthropic stream clients."""
    complete_response = {
        "id": "chatcmpl-complete-anthropic",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "complete response",
                "tool_calls": [{
                    "id": "call_read",
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
    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = complete_response
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "openai::gpt-4o",
                "max_tokens": 128,
                "stream": True,
                "tools": [{
                    "name": "read",
                    "input_schema": {"type": "object"},
                }],
                "messages": [{"role": "user", "content": "read README"}],
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        content = await resp.read()

    assert b"event: message_start" in content
    assert b"complete response" in content
    assert b'"type": "tool_use"' in content
    assert b'"id": "call_read"' in content
    assert b"README.md" in content
    assert b"event: message_stop" in content


@pytest.mark.asyncio
async def test_messages_forwards_client_session_to_provider_call(client):
    openai_response = {
        "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}],
        "usage": {},
    }
    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = openai_response
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "opencode-go::minimax-m3",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers={**HEADERS_AUTH, "x-opencode-session": "anthropic-session-1"},
        )

    assert resp.status == 200
    assert mock_chat.call_args.kwargs["conversation_id"] == "anthropic-session-1"


@pytest.mark.asyncio
async def test_messages_forwards_parameters_and_tool_history(client):
    openai_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "done",
            },
            "finish_reason": "stop",
        }],
        "usage": {},
    }
    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = openai_response
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "openai::gpt-4o",
                "max_tokens": 256,
                "temperature": 0.2,
                "top_p": 0.8,
                "stop_sequences": ["END"],
                "tools": [{
                    "name": "read",
                    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                }],
                "messages": [
                    {"role": "user", "content": "inspect"},
                    {
                        "role": "assistant",
                        "content": [{
                            "type": "tool_use",
                            "id": "toolu_read",
                            "name": "read",
                            "input": {"path": "README.md"},
                        }],
                    },
                    {
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": "toolu_read",
                            "content": "contents",
                        }],
                    },
                ],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    kwargs = mock_chat.call_args.kwargs
    assert kwargs["tools"][0]["function"]["name"] == "read"
    assert kwargs["extra_body"] == {
        "max_tokens": 256,
        "temperature": 0.2,
        "top_p": 0.8,
        "stop": ["END"],
    }
    messages = mock_chat.call_args.args[2]
    assert messages[1]["tool_calls"][0]["function"]["name"] == "read"
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "toolu_read",
        "content": "contents",
    }


@pytest.mark.asyncio
async def test_messages_pool_image_requirement_is_preserved_across_fallbacks(app, client):
    pool_manager = MagicMock()
    pool_manager.select.side_effect = [
        ("xiaomi", "mimo-v2.5"),
        ("openai", "gpt-4o"),
    ]
    app["pool_manager"] = pool_manager
    openai_response = {
        "choices": [{"message": {"content": "seen"}, "finish_reason": "stop"}],
        "usage": {},
    }

    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.side_effect = [RuntimeError("first provider failed"), openai_response]
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "hermes-code",
                "max_tokens": 100,
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "AAAA",
                        },
                    }],
                }],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    assert pool_manager.select.call_args_list == [
        call("code", excluded=set(), required_input_modalities=frozenset({"image"})),
        call(
            "code",
            excluded={("xiaomi", "mimo-v2.5")},
            required_input_modalities=frozenset({"image"}),
        ),
    ]


@pytest.mark.asyncio
async def test_messages_pool_image_requirement_is_preserved_after_breaker_skip(app, client):
    pool_manager = MagicMock()
    pool_manager.select.side_effect = [
        ("xiaomi", "mimo-v2.5-pro"),
        ("xiaomi", "mimo-v2.5"),
    ]
    app["pool_manager"] = pool_manager
    breaker = MagicMock()
    breaker.check.side_effect = [
        MagicMock(allowed=False),
        MagicMock(allowed=True),
    ]
    app["breaker"] = breaker

    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"content": "seen"}, "finish_reason": "stop"}],
            "usage": {},
        }
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "hermes-code",
                "max_tokens": 100,
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "image",
                        "source": {"type": "url", "url": "https://example.test/image.png"},
                    }],
                }],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    assert pool_manager.select.call_args_list == [
        call("code", excluded=set(), required_input_modalities=frozenset({"image"})),
        call(
            "code",
            excluded={("xiaomi", "mimo-v2.5-pro")},
            required_input_modalities=frozenset({"image"}),
        ),
    ]


@pytest.mark.asyncio
async def test_messages_text_only_pool_request_has_no_modality_requirement(app, client):
    pool_manager = MagicMock()
    pool_manager.select.return_value = ("openai", "gpt-4o-mini")
    app["pool_manager"] = pool_manager

    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {},
        }
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "hermes-code",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    pool_manager.select.assert_called_once_with(
        "code",
        excluded=set(),
        required_input_modalities=None,
    )


@pytest.mark.asyncio
async def test_messages_image_passthrough_does_not_select_from_pool(app, client):
    pool_manager = MagicMock()
    app["pool_manager"] = pool_manager

    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"content": "seen"}, "finish_reason": "stop"}],
            "usage": {},
        }
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "openai::gpt-4o",
                "max_tokens": 100,
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "image",
                        "source": {"type": "url", "url": "https://example.test/image.png"},
                    }],
                }],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    pool_manager.select.assert_not_called()
    assert mock_chat.call_args.args[:2] == ("openai", "gpt-4o")


@pytest.mark.asyncio
async def test_messages_streaming(client):
    """Streaming request returns Anthropic SSE events."""
    async def mock_openai_stream(*args, **kwargs):
        yield b'data: {"choices": [{"delta": {"content": "Hi"}, "finish_reason": null}]}\n\n'
        yield b'data: {"choices": [{"delta": {"content": " there"}, "finish_reason": null}]}\n\n'
        yield b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
        yield b'data: [DONE]\n\n'

    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = mock_openai_stream()
        resp = await client.post("/v1/messages", json={
            "model": "claude-3-opus-20240229",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }, headers=HEADERS_AUTH)
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"

        content = await resp.read()
        # Verify key Anthropic SSE events are present.
        assert b"event: message_start" in content
        assert b"event: content_block_start" in content
        assert b'"text_delta"' in content
        assert b'"Hi"' in content
        assert b" there" in content
        assert b"event: content_block_stop" in content
        assert b"event: message_delta" in content
        assert b"event: message_stop" in content


@pytest.mark.asyncio
async def test_messages_streaming_emits_accel_buffering_header(client):
    """Streaming response must disable proxy buffering."""
    async def mock_stream(*args, **kwargs):
        yield b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": null}]}\n\n'
        yield b'data: [DONE]\n\n'

    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = mock_stream()
        resp = await client.post("/v1/messages", json={
            "model": "claude-3",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }, headers=HEADERS_AUTH)
        assert resp.headers.get("X-Accel-Buffering") == "no"


@pytest.mark.asyncio
async def test_messages_with_system_prompt(client):
    """System prompt is converted and forwarded."""
    openai_response = {
        "choices": [{"message": {"content": "acknowledged"}, "finish_reason": "stop"}],
        "usage": {},
    }
    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = openai_response
        resp = await client.post("/v1/messages", json={
            "model": "claude-3",
            "max_tokens": 100,
            "system": "Be concise.",
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers=HEADERS_AUTH)
        assert resp.status == 200
        # Verify the system message was prepended.
        args, kwargs = mock_chat.call_args
        messages = args[2] if len(args) > 2 else kwargs.get("messages", [])
        assert messages[0] == {"role": "system", "content": "Be concise."}


@pytest.mark.asyncio
async def test_messages_with_conversation_history(client):
    """Multi-turn conversation is forwarded correctly."""
    openai_response = {
        "choices": [{"message": {"content": "continuing"}, "finish_reason": "stop"}],
        "usage": {},
    }
    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = openai_response
        resp = await client.post("/v1/messages", json={
            "model": "claude-3",
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
                {"role": "user", "content": "How are you?"},
            ],
        }, headers=HEADERS_AUTH)
        assert resp.status == 200
        args, kwargs = mock_chat.call_args
        messages = args[2] if len(args) > 2 else kwargs.get("messages", [])
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"


@pytest.mark.asyncio
async def test_messages_provider_error(client):
    """Provider error returns Anthropic-format error response."""
    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.side_effect = Exception("upstream timeout")
        resp = await client.post("/v1/messages", json={
            "model": "claude-3",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers=HEADERS_AUTH)
        assert resp.status == 502
        data = await resp.json()
        assert data["type"] == "error"
        assert data["error"]["type"] == "api_error"
        assert "upstream timeout" in data["error"]["message"]


@pytest.mark.asyncio
async def test_messages_empty_pool_selection_is_retryable(app, client, monkeypatch):
    """Anthropic clients receive the same retryable pool-exhaustion signal."""
    pool_manager = MagicMock()
    pool_manager.select.return_value = None
    app["pool_manager"] = pool_manager
    monkeypatch.setenv("TUSKER_PROVIDER_RETRY_AFTER_SECS", "6")

    with patch("tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "hermes-code",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=HEADERS_AUTH,
        )
        data = await resp.json()

    assert resp.status == 503
    assert resp.headers["Retry-After"] == "6"
    assert data == {
        "type": "error",
        "error": {
            "type": "server_error",
            "message": "No healthy upstream model is currently available; retry shortly.",
        },
    }
    mock_chat.assert_not_called()
