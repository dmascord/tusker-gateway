"""Regression tests for redaction-aware SSE ordering.

All fixtures are synthetic: never use live email addresses or credentials.
"""
import asyncio
import json

import pytest

from tusker_gateway.endpoints import _normalize_stream
from tusker_gateway.redaction import Tokenizer, RedactionRuleSet, _BufferedRestorer


def _tokenizer():
    return Tokenizer(key=b"0123456789abcdef0123456789abcdef", rules=RedactionRuleSet())


def _event(delta=None, finish=None):
    return ("data: " + json.dumps({
        "id": "test", "object": "chat.completion.chunk", "model": "test",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    }) + "\n\n").encode()


async def _replay(events, split):
    wire = b"".join(events) + b"data: [DONE]\n\n"
    async def upstream():
        for i in range(0, len(wire), split):
            yield wire[i:i + split]
    return [json.loads(f[6:]) for f in [
        frame async for frame in _normalize_stream(
            upstream(), provider="test", model="test", restorer=_tokenizer()
        )
    ] if f.startswith(b"data: {")]


def _deltas(events, field):
    return "".join(str(choice["delta"].get(field, "")) for event in events
                   for choice in event.get("choices", []))


@pytest.mark.parametrize("split", [1, 2, 3, 7, 31, 4096])
def test_plain_content_preserves_order_across_network_splits(split):
    parts = ["Hello", " world", ", how", " are you?"]
    events = asyncio.run(_replay([_event({"content": p}) for p in parts]
                                 + [_event(finish="stop")], split))
    assert _deltas(events, "content") == "".join(parts)
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("split", [1, 2, 7, 31, 4096])
def test_token_split_with_interleaved_reasoning_does_not_cross_contaminate(split):
    t = _tokenizer()
    email = "synthetic.user@example.test"
    token = t.tokenize_text(email)
    middle = len(token) // 2
    frames = [
        _event({"content": "Contact " + token[:middle]}),
        _event({"reasoning_content": "Reasoning begins "}),
        _event({"content": token[middle:] + " today."}),
        _event({"reasoning_content": "and ends."}),
        _event(finish="stop"),
    ]
    events = asyncio.run(_replay(frames, split))
    assert _deltas(events, "content") == "Contact " + email + " today."
    assert _deltas(events, "reasoning_content") == "Reasoning begins and ends."
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("split", [1, 7, 97])
def test_terminal_done_without_finish_drains_text_and_arguments(split):
    t = _tokenizer()
    email = "synthetic.user@example.test"
    token = t.tokenize_text(email)
    frames = [_event({"content": "Hello " + token}),
              _event({"content": " final"})]
    events = asyncio.run(_replay(frames, split))
    assert _deltas(events, "content") == "Hello " + email + " final"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("size", [1, 7, 31, 32, 33, 1024])
def test_plain_words_are_not_delayed_until_next_word(size):
    r = _BufferedRestorer(_tokenizer())
    text = "a" * size
    output = r.feed(text)
    assert output + r.flush() == text
    if size < 32:
        assert output == text


def test_bare_authenticated_payload_survives_fragmentation():
    t = _tokenizer()
    email = "synthetic.user@example.test"
    payload = t.tokenize_text(email).split(":")[-1][:-1]
    for chunk in (1, 3, 7, 19, 100):
        r = _BufferedRestorer(t)
        wire = "lookup " + payload + " now"
        output = "".join(r.feed(wire[i:i+chunk])
                         for i in range(0, len(wire), chunk)) + r.flush()
        assert output == "lookup " + email + " now"
