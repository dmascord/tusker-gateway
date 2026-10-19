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

@pytest.mark.asyncio
@pytest.mark.parametrize("split", [1, 7, 97])
async def test_legacy_done_without_finish_drains_tail_before_synthesized_stop(split):
    """A [DONE]-terminated legacy stream synthesizes the finish frame the
    upstream omitted; the held restorer tail must precede it."""
    from tusker_gateway.redaction import Tokenizer, RedactionRuleSet

    t = Tokenizer(key=b"0123456789abcdef0123456789abcdef", rules=RedactionRuleSet())
    email = "synthetic.user@example.test"
    token = t.tokenize_text(email)
    wire = b"".join([
        frame({"content": "Hello " + token[:len(token)//2]}),
        frame({"content": token[len(token)//2:] + " final"}),
        b"data: [DONE]\n\n",
    ])
    async def upstream():
        for i in range(0, len(wire), split):
            yield wire[i:i + split]
    events = []
    async for chunk in _normalize_stream_legacy(upstream(), restorer=t):
        for line in chunk.splitlines():
            if line.startswith(b"data: ") and b"[DONE]" not in line:
                events.append(json.loads(line[6:]))
    text = "".join(c["delta"].get("content", "") for e in events for c in e["choices"])
    assert text == "Hello " + email + " final"
    finish_chunks = [c for e in events for c in e["choices"] if c.get("finish_reason")]
    assert finish_chunks == [{"index": 0, "delta": {}, "finish_reason": "stop"}]


@pytest.mark.asyncio
async def test_legacy_interleaved_reasoning_and_content_drain_ordering():
    """Legacy path: per-field carry buffers drain before the finish chunk,
    and a split token in one channel never consumes the other's bytes."""
    from tusker_gateway.redaction import Tokenizer, RedactionRuleSet

    t = Tokenizer(key=b"0123456789abcdef0123456789abcdef", rules=RedactionRuleSet())
    email = "synthetic.user@example.test"
    token = t.tokenize_text(email)
    mid = len(token) // 2
    wire = b"".join([
        frame({"reasoning_content": "think " + token[:mid]}),
        frame({"content": "see " + token[:mid]}),
        frame({"reasoning_content": token[mid:]}),
        frame({"content": token[mid:]}),
        frame({}, "stop"),
        b"data: [DONE]\n\n",
    ])
    async def upstream():
        yield wire
    events = []
    async for chunk in _normalize_stream_legacy(upstream(), restorer=t):
        for line in chunk.splitlines():
            if line.startswith(b"data: ") and b"[DONE]" not in line:
                events.append(json.loads(line[6:]))
    def text(field):
        return "".join(c["delta"].get(field, "") for e in events for c in e["choices"])
    assert text("reasoning_content") == "think " + email
    assert text("content") == "see " + email
    assert [c["finish_reason"] for e in events for c in e["choices"] if c.get("finish_reason")] == ["stop"]
