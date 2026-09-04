"""Tests for the format translator registry and built-in Anthropic translator.

The registry design (borrowed from 9router) lets each translator module
self-register on import. These tests verify:

- registry mechanics (register, lookup, override)
- request translation: Anthropic → OpenAI
- response translation: OpenAI → Anthropic
- streaming: state init, message_start, text_delta, closing sequence,
  first-chunk text bundled with message_start
"""
from __future__ import annotations

import json

import pytest

from tusker_gateway import translators
from tusker_gateway.translators import (
    ANTHROPIC,
    OPENAI,
    init_streaming_state,
    register_request_override,
    stream_chunk,
    translate_request,
    translate_response,
)
from tusker_gateway.translators.anthropic import (
    init_anthropic_stream_state,
    response_openai_to_anthropic,
    translate_openai_chunk_to_anthropic,
    update_stream_state,
)


# ---------------------------------------------------------------------------
# Registry mechanics
# ---------------------------------------------------------------------------


def test_registry_has_anthropic_after_import():
    """Importing the translators package should auto-register the Anthropic translator."""
    assert ANTHROPIC in translators._request_translators
    assert ANTHROPIC in translators._response_translators
    assert ANTHROPIC in translators._stream_chunk_translators


def test_translate_request_openai_passthrough():
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
    assert translate_request(OPENAI, body) is body


def test_translate_response_openai_passthrough():
    chunk = {"id": "1", "choices": []}
    out = translate_response(OPENAI, chunk, {})
    assert out == [chunk]


def test_register_request_duplicate_raises():
    with pytest.raises(ValueError, match="already registered"):
        translators.register_request("nonexistent-source", lambda b: b)
        translators.register_request("nonexistent-source", lambda b: b)


def test_register_request_override_replaces():
    def stub(body):
        return {"replaced": True}

    try:
        register_request_override("replace-test-source", stub)
        assert translate_request("replace-test-source", {"x": 1}) == {"replaced": True}
    finally:
        translators._request_translators.pop("replace-test-source", None)


# ---------------------------------------------------------------------------
# Request: Anthropic → OpenAI
# ---------------------------------------------------------------------------


class TestRequestAnthropicToOpenAI:
    def test_basic_conversion(self):
        body = {
            "model": "claude-3",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        out = translate_request(ANTHROPIC, body)
        assert out["model"] == "claude-3"
        assert out["max_tokens"] == 1024
        assert out["messages"] == [{"role": "user", "content": "Hello"}]

    def test_system_string(self):
        body = {"system": "You are helpful.", "messages": []}
        out = translate_request(ANTHROPIC, body)
        assert out["messages"][0] == {"role": "system", "content": "You are helpful."}

    def test_system_unknown_block_is_not_silently_dropped(self):
        body = {
            "system": [{"type": "tool_use", "id": "unexpected", "name": "read", "input": {}}],
            "messages": [],
        }

        out = translate_request(ANTHROPIC, body)

        assert "unexpected" in out["messages"][0]["content"]

    def test_stop_sequences_mapping(self):
        body = {"stop_sequences": ["END", "STOP"], "messages": []}
        out = translate_request(ANTHROPIC, body)
        assert out["stop"] == ["END", "STOP"]

    def test_tools_with_input_schema(self):
        body = {
            "messages": [],
            "tools": [{
                "name": "bash",
                "description": "run shell",
                "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            }],
        }
        out = translate_request(ANTHROPIC, body)
        assert out["tools"] == [{
            "type": "function",
            "function": {
                "name": "bash",
                "description": "run shell",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            },
        }]

    def test_image_base64_source(self):
        body = {
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "AAA"},
                }],
            }],
        }
        out = translate_request(ANTHROPIC, body)
        block = out["messages"][0]["content"][0]
        assert block == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAA"},
        }

    def test_stream_flag_passthrough(self):
        body = {"stream": True, "messages": []}
        assert translate_request(ANTHROPIC, body)["stream"] is True

    def test_tool_use_and_tool_result_remain_structured(self):
        body = {
            "messages": [
                {"role": "user", "content": "read the file"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will inspect it."},
                        {
                            "type": "tool_use",
                            "id": "toolu_read",
                            "name": "read",
                            "input": {"path": "package.json"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "toolu_read",
                        "content": [{"type": "text", "text": "{}"}],
                        "is_error": True,
                    }],
                },
            ],
        }

        out = translate_request(ANTHROPIC, body)

        assert out["messages"][1] == {
            "role": "assistant",
            "content": "I will inspect it.",
            "tool_calls": [{
                "id": "toolu_read",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": '{"path": "package.json"}',
                },
            }],
        }
        assert out["messages"][2] == {
            "role": "tool",
            "tool_call_id": "toolu_read",
            "content": "{}",
            "is_error": True,
        }


# ---------------------------------------------------------------------------
# Response: OpenAI → Anthropic
# ---------------------------------------------------------------------------


class TestResponseOpenAIToAnthropic:
    def test_basic_text(self):
        result = {
            "choices": [{
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
        chunks = translate_response(ANTHROPIC, result, {"original_model": "claude-3"})
        assert len(chunks) == 1
        resp = chunks[0]
        assert resp["role"] == "assistant"
        assert resp["content"][0]["text"] == "hi"
        assert resp["stop_reason"] == "end_turn"
        assert resp["model"] == "claude-3"
        assert resp["usage"] == {"input_tokens": 5, "output_tokens": 3}

    def test_tool_calls_become_tool_use_blocks(self):
        result = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "function": {
                            "name": "bash",
                            "arguments": '{"cmd": "ls"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }
        chunks = translate_response(ANTHROPIC, result, {"original_model": "claude-3"})
        block = chunks[0]["content"][0]
        assert block["type"] == "tool_use"
        assert block["name"] == "bash"
        assert block["input"] == {"cmd": "ls"}
        assert chunks[0]["stop_reason"] == "tool_use"

    def test_tool_calls_with_dict_arguments(self):
        """Some providers return arguments as a dict, not a JSON string."""
        result = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {"name": "f", "arguments": {"x": 1}},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }
        chunks = translate_response(ANTHROPIC, result, {})
        assert chunks[0]["content"][0]["input"] == {"x": 1}

    def test_legacy_function_call_becomes_tool_use_block_without_normalizer(self):
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

        response = translate_response(ANTHROPIC, result, {})[0]

        block = response["content"][0]
        assert block["type"] == "tool_use"
        assert block["name"] == "read"
        assert block["input"] == {"path": "README.md"}
        assert response["stop_reason"] == "tool_use"

    def test_finish_reason_length(self):
        result = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        chunks = translate_response(ANTHROPIC, result, {})
        assert chunks[0]["stop_reason"] == "max_tokens"

    def test_preserves_output_blocks_and_usage_aliases(self):
        result = {
            "choices": [{
                "message": {
                    "content": [
                        {"type": "text", "text": "done"},
                        {"type": "provider_annotation", "value": "kept"},
                    ],
                },
                "finish_reason": "stop",
            }],
            "usage": {"input_tokens": 7, "output_tokens": 4},
        }

        response = translate_response(ANTHROPIC, result, {})[0]

        assert response["content"] == [
            {"type": "text", "text": "done"},
            {
                "type": "text",
                "text": '{"type": "provider_annotation", "value": "kept"}',
            },
        ]
        assert response["usage"] == {"input_tokens": 7, "output_tokens": 4}


# ---------------------------------------------------------------------------
# Streaming: state init + chunk translator
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_init_state_defaults(self):
        state = init_anthropic_stream_state()
        assert state["model"] == ""
        assert state["started"] is False
        assert state["closed"] is False
        assert state["input_tokens"] == 0

    def test_init_state_with_kwargs(self):
        state = init_anthropic_stream_state(
            model="claude-3", input_tokens=42,
            msg_id="msg_custom",
        )
        assert state["model"] == "claude-3"
        assert state["input_tokens"] == 42
        assert state["msg_id"] == "msg_custom"

    def test_update_stream_state(self):
        state = init_anthropic_stream_state()
        update_stream_state(
            state, model="claude-3", input_tokens=10, msg_id="msg_x",
        )
        assert state["model"] == "claude-3"
        assert state["input_tokens"] == 10
        assert state["msg_id"] == "msg_x"

    def test_first_chunk_emits_start_and_content(self):
        """First chunk yields message_start + content_block_start + text if present."""
        state = init_anthropic_stream_state()
        update_stream_state(state, model="claude-3", input_tokens=5)
        frames = translate_openai_chunk_to_anthropic(
            {"choices": [{"delta": {"content": "hi"}}]},
            state,
        )
        text = b"".join(frames).decode("utf-8")
        assert "event: message_start" in text
        assert "event: content_block_start" in text
        assert "text_delta" in text and "hi" in text

    def test_first_chunk_empty_still_emits_start(self):
        state = init_anthropic_stream_state()
        update_stream_state(state, model="claude-3")
        frames = translate_openai_chunk_to_anthropic(
            {"choices": [{"delta": {"role": "assistant"}}]},
            state,
        )
        text = b"".join(frames).decode("utf-8")
        assert "event: message_start" in text
        assert "event: content_block_start" in text

    def test_subsequent_chunk_emits_text_delta(self):
        state = init_anthropic_stream_state()
        update_stream_state(state, model="claude-3")
        # First chunk to start the stream.
        translate_openai_chunk_to_anthropic(
            {"choices": [{"delta": {}}]}, state,
        )
        # Second chunk with content.
        frames = translate_openai_chunk_to_anthropic(
            {"choices": [{"delta": {"content": " world"}}]}, state,
        )
        text = b"".join(frames).decode("utf-8")
        assert "text_delta" in text and " world" in text
        # No message_start in subsequent frames.
        assert "event: message_start" not in text

    def test_none_chunk_emits_closing_sequence(self):
        state = init_anthropic_stream_state()
        update_stream_state(state, model="claude-3", input_tokens=5)
        translate_openai_chunk_to_anthropic(
            {"choices": [{"delta": {"content": "hi"}}]}, state,
        )
        # Close.
        frames = translate_openai_chunk_to_anthropic(None, state)
        text = b"".join(frames).decode("utf-8")
        assert "event: content_block_stop" in text
        assert "event: message_delta" in text
        assert "event: message_stop" in text

    def test_close_after_close_is_noop(self):
        state = init_anthropic_stream_state()
        update_stream_state(state, model="claude-3")
        translate_openai_chunk_to_anthropic(
            {"choices": [{"delta": {"content": "hi"}}]}, state,
        )
        first_close = translate_openai_chunk_to_anthropic(None, state)
        second_close = translate_openai_chunk_to_anthropic(None, state)
        assert len(first_close) > 0
        assert second_close == []

    def test_chunk_after_close_dropped(self):
        state = init_anthropic_stream_state()
        update_stream_state(state, model="claude-3")
        translate_openai_chunk_to_anthropic(
            {"choices": [{"delta": {"content": "hi"}}]}, state,
        )
        translate_openai_chunk_to_anthropic(None, state)
        late = translate_openai_chunk_to_anthropic(
            {"choices": [{"delta": {"content": "late"}}]}, state,
        )
        assert late == []

    def test_finish_reason_records_stop(self):
        state = init_anthropic_stream_state()
        update_stream_state(state, model="claude-3")
        translate_openai_chunk_to_anthropic({"choices": [{"delta": {}}]}, state)
        translate_openai_chunk_to_anthropic(
            {"choices": [{"finish_reason": "stop"}]}, state,
        )
        closing = translate_openai_chunk_to_anthropic(None, state)
        text = b"".join(closing).decode("utf-8")
        assert '"stop_reason": "end_turn"' in text

    def test_native_tool_call_stream_preserves_id_name_and_arguments(self):
        state = init_anthropic_stream_state(model="claude-3")

        first = translate_openai_chunk_to_anthropic(
            {
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_read",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path":'},
                        }],
                    },
                }],
            },
            state,
        )
        second = translate_openai_chunk_to_anthropic(
            {
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "function": {"arguments": '"package.json"}'},
                            },
                        ],
                    },
                }],
            },
            state,
        )
        closing = translate_openai_chunk_to_anthropic(
            {"choices": [{"finish_reason": "tool_calls", "delta": {}}]},
            state,
        )
        closing.extend(translate_openai_chunk_to_anthropic(None, state))

        stream_text = b"".join([*first, *second, *closing]).decode("utf-8")
        assert '"type": "tool_use"' in stream_text
        assert '"id": "call_read"' in stream_text
        assert '"name": "read"' in stream_text
        assert '"partial_json": "{\\"path\\":"' in stream_text
        assert '"partial_json": "\\"package.json\\"}"' in stream_text
        assert '"stop_reason": "tool_use"' in stream_text

    def test_via_registry_init_streaming_state(self):
        state = init_streaming_state(ANTHROPIC)
        assert state["started"] is False
        # No-op for unregistered targets.
        assert init_streaming_state("nonexistent") == {}

    def test_via_registry_stream_chunk(self):
        state = init_streaming_state(ANTHROPIC)
        update_stream_state(state, model="claude-3", input_tokens=5)
        frames = stream_chunk(
            ANTHROPIC,
            {"choices": [{"delta": {"content": "x"}}]},
            state,
        )
        text = b"".join(frames).decode("utf-8")
        assert "event: message_start" in text
        assert "x" in text
