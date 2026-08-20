"""Tests for SSE framing and OpenAI object builders."""
from __future__ import annotations

import json
from tusker_gateway.sse import sse_frame, sse_done, format_openai_chunk, format_openai_response


def test_sse_framing():
    data = {"foo": "bar"}
    frame = sse_frame(data)
    assert frame == b'data: {"foo": "bar"}\n\n'
    assert sse_done() == b"data: [DONE]\n\n"


def test_format_openai_chunk():
    chunk = format_openai_chunk(content="hello", role="assistant")
    assert "choices" in chunk
    assert chunk["choices"][0]["delta"]["content"] == "hello"
    assert chunk["choices"][0]["delta"]["role"] == "assistant"
    assert chunk["model"] == "tusker-gateway"


def test_format_openai_response():
    resp = format_openai_response(content="full text", model="my-model")
    assert resp["object"] == "chat.completion"
    assert resp["choices"][0]["message"]["content"] == "full text"
    assert resp["model"] == "my-model"
