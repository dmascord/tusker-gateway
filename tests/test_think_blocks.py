"""Tests for inline <think>-block normalisation in provider responses.

Providers like DeepSeek R1 and MiniMax M-series emit ``
blocks inline with the visible answer. The normalizer must move
every `` block into `` so OpenAI-compatible
clients see a uniform response shape.
"""

from __future__ import annotations

import json

import pytest

from tusker_gateway.tool_formats import (
    _split_think_blocks,
    normalize_response_tool_calls,
)


class TestSplitThinkBlocks:
    def test_passthrough_when_no_think_block(self):
        visible, reasoning = _split_think_blocks("Hello, world!")
        assert visible == "Hello, world!"
        assert reasoning is None

    def test_extracts_single_block(self):
        visible, reasoning = _split_think_blocks("<think>some private thought</think>\n\nPONG")
        assert visible == "PONG"
        assert reasoning == "some private thought"

    def test_extracts_multiple_blocks(self):
        content = "<think>first thought</think>\nbetween\n<think>second thought</think>\n\nPONG"
        visible, reasoning = _split_think_blocks(content)
        # Visible: text with both blocks removed and surrounding whitespace
        # collapsed by strip(). The literal "between" between the blocks is
        # preserved.
        assert "PONG" in visible
        assert "between" in visible
        assert "<think>" not in visible
        assert reasoning == "first thought\n\nsecond thought"

    def test_only_thinking_returns_empty_visible(self):
        visible, reasoning = _split_think_blocks("<think>everything</think>")
        assert visible == ""
        assert reasoning == "everything"

    def test_preserves_internal_newlines(self):
        content = "<think>line1\nline2\nline3</think>\n\nAnswer"
        visible, reasoning = _split_think_blocks(content)
        assert reasoning == "line1\nline2\nline3"
        assert visible == "Answer"

    def test_case_insensitive_tag(self):
        visible, reasoning = _split_think_blocks("<THINK>foo</THINK>\n\nPONG")
        assert reasoning == "foo"
        assert visible == "PONG"


class TestNormalizeExtractsThinkBlocks:
    def test_minimax_response_shape(self):
        """Real MiniMax-M3 payload shape -> visible separated from thinking."""
        response = {
            "id": "06f43bc0a013fc73d69d9163fc3ba7e5",
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {
                        "content": (
                            '<think>The user wants me to reply with "PONG".'
                            " This is a simple request.\n</think>\n\nPONG"
                        ),
                        "role": "assistant",
                    },
                }
            ],
            "model": "MiniMax-M3",
        }
        out = normalize_response_tool_calls(response, source="minimax")
        msg = out["choices"][0]["message"]
        assert msg["content"] == "PONG"
        assert msg["reasoning_content"] == (
            'The user wants me to reply with "PONG". This is a simple request.'
        )
        # ``text()`` should not appear in the visible field, even once.
        assert "<think>" not in msg["content"]
        assert "<think>" not in msg["reasoning_content"]

    def test_preserves_provider_supplied_reasoning_content(self):
        """If the provider already returned ``reasoning_content`` we
        append our extracted block to it rather than overwriting.
        """
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {
                        "content": "<think>inner chain of thought</think>\n\nanswer",
                        "reasoning_content": "provider-supplied thinking",
                        "role": "assistant",
                    },
                }
            ]
        }
        out = normalize_response_tool_calls(response)
        msg = out["choices"][0]["message"]
        assert msg["content"] == "answer"
        # Both blocks present, separated by a blank line.
        assert "provider-supplied thinking" in msg["reasoning_content"]
        assert "inner chain of thought" in msg["reasoning_content"]

    def test_response_without_think_block_unchanged(self):
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {"content": "hello world", "role": "assistant"},
                }
            ]
        }
        out = normalize_response_tool_calls(response)
        assert out["choices"][0]["message"]["content"] == "hello world"
        # No spurious reasoning_content field injected.
        assert "reasoning_content" not in out["choices"][0]["message"]

    def test_multiple_blocks_combined(self):
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {
                        "content": ("<think>first</think>\nmiddle\n<think>second</think>\nPONG"),
                        "role": "assistant",
                    },
                }
            ]
        }
        out = normalize_response_tool_calls(response)
        msg = out["choices"][0]["message"]
        # "middle" between blocks remains visible.
        assert "middle" in msg["content"]
        assert "PONG" in msg["content"]
        assert "<think>" not in msg["content"]
        # Both inner payloads joined into reasoning_content with separator.
        rc = msg["reasoning_content"]
        assert "first" in rc
        assert "second" in rc
