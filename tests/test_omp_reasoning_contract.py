"""OMP-facing OpenAI stream contract regression fixtures.

These tests are intentionally not run as part of this patch creation.
"""
import json

import pytest

from tusker_gateway.endpoints import _normalize_stream, _normalize_stream_legacy


def frame(delta, finish=None):
    return ("data: " + json.dumps({
        "id": "chatcmpl-omp", "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }) + "\n\n").encode()


async def upstream(*frames):
    for item in frames:
        yield item


async def collect(normalizer, *frames):
    output = []
    async for chunk in normalizer(upstream(*frames)):
        for line in chunk.splitlines():
            if line.startswith(b"data:"):
                value = line[5:].strip()
                if value != b"[DONE]":
                    output.append(json.loads(value))
    return output


def deltas(events):
    return [choice["delta"] for event in events
            for choice in event.get("choices", [])]


@pytest.mark.asyncio
async def test_reasoning_only_kept_separate():
    events = await collect(
        _normalize_stream,
        frame({"reasoning_content": "think first", "content": None}),
        frame({"content": "answer"}, "stop"),
        b"data: [DONE]\n\n",
    )
    ds = deltas(events)
    assert any(d.get("reasoning_content") == "think first" for d in ds)
    assert not any(d.get("content") == "think first" for d in ds)
    assert any(d.get("content") == "answer" for d in ds)


@pytest.mark.asyncio
async def test_reasoning_alias_normalized_once():
    events = await collect(
        _normalize_stream,
        frame({"reasoning": "thought", "reasoning_details": [
            {"type": "reasoning.text", "text": "thought"}]}),
        frame({"content": "done"}, "stop"),
        b"data: [DONE]\n\n",
    )
    ds = deltas(events)
    assert any(d.get("reasoning_content") == "thought" for d in ds)
    assert not any(d.get("content") == "thought" for d in ds)
    assert not any(d.get("reasoning") == "thought" for d in ds)


@pytest.mark.asyncio
async def test_reasoning_and_native_tools_remain_distinct():
    events = await collect(
        _normalize_stream,
        frame({"reasoning_content": "call tool"}),
        frame({"tool_calls": [{"index": 0, "id": "call_omp",
            "type": "function", "function": {"name": "search", "arguments": "{}"}}]}, "tool_calls"),
        b"data: [DONE]\n\n",
    )
    ds = deltas(events)
    assert any(d.get("reasoning_content") == "call tool" for d in ds)
    assert any(d.get("tool_calls") for d in ds)
    assert not any(d.get("content") == "call tool" for d in ds)


@pytest.mark.asyncio
async def test_legacy_reasoning_finish_is_separate():
    events = await collect(
        _normalize_stream_legacy,
        frame({"reasoning_content": "thought"}, "stop"),
        b"data: [DONE]\n\n",
    )
    choices = [c for e in events for c in e.get("choices", [])]
    assert any(c["delta"].get("reasoning_content") == "thought"
               and c["finish_reason"] is None for c in choices)
    assert any(c["finish_reason"] == "stop" and not c["delta"] for c in choices)
