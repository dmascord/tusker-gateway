"""The gateway must restore redaction tokens inside streamed tool-call
arguments without breaking the surrounding JSON.

A redaction token can stand in for a secret whose plaintext contains JSON
metacharacters (a double quote, a backslash, a control character). Substituting
that plaintext into a ``function.arguments`` JSON string with raw-text
replacement terminates or corrupts the surrounding document. Every upstream
that re-parses the arguments -- and OMP when it replays the transcript on the
next turn -- then rejects the turn with ``invalid tool call arguments`` /
``Invalid \\escape``.

Both restore paths are exercised:

* native OpenAI deltas -- ``delta.tool_calls[*].function.arguments``
* text rescue -- ``<gateway_tool_call>`` json embedded in assistant content

Invariants asserted on the arguments the client assembles:

1. the assembled string parses as JSON;
2. decoding it yields the original sealed plaintext byte-for-byte.
"""
from __future__ import annotations

import json

import pytest

from tusker_gateway.endpoints import _normalize_stream
from tusker_gateway.redaction import (
    TOKEN_TYPE_TEXT,
    RedactionRuleSet,
    Tokenizer,
)


def _tokenizer() -> Tokenizer:
    return Tokenizer(
        key=b"0123456789abcdef0123456789abcdef", rules=RedactionRuleSet()
    )


# Plaintext with every character class that breaks a JSON string literal when
# substituted unescaped: a double quote, a backslash, and a newline.
_SECRET_PLAINTEXT = 'p@ss"word \\ path\nsecond line'


def _sealed() -> str:
    """A token standing in for a secret that breaks JSON when restored raw."""
    tok = _tokenizer()
    return tok._seal(_SECRET_PLAINTEXT, TOKEN_TYPE_TEXT)


def _calls_from_frames(frames: list[bytes]) -> list[dict]:
    calls: list[dict] = []
    for frame in frames:
        if not frame.startswith(b"data: "):
            continue
        stripped = frame.removeprefix(b"data: ").strip()
        if not stripped or stripped == b"[DONE]":
            continue
        try:
            event = json.loads(stripped)
        except Exception:
            continue
        for choice in event.get("choices") or []:
            for call in (choice.get("delta", {}) or {}).get("tool_calls") or []:
                calls.append(call)
    return calls


async def _run(events: list[bytes]) -> list[bytes]:
    async def upstream():
        for event in events:
            yield event
        yield b"data: [DONE]\n\n"

    return [
        frame
        async for frame in _normalize_stream(
            upstream(),
            provider="claude-code-cli",
            model="sonnet",
            tools_requested=True,
            restorer=_tokenizer(),
        )
    ]


def test_sealed_secret_breaks_json_when_restored_raw():
    """Guard against a vacuous test: the sealed plaintext really does make the
    surrounding JSON invalid if substituted without escaping."""
    token = _sealed()
    document = '{"command": "printf ' + token + '"}'
    assert json.loads(document), "the sealed document itself must be valid JSON"
    corrupt = _tokenizer().restore_text(document)
    with pytest.raises(json.JSONDecodeError):
        json.loads(corrupt)


@pytest.mark.asyncio
async def test_native_argument_json_parses_when_sealed_value_has_metachars():
    """Native streamed deltas keep ``arguments`` parseable across restore."""
    token = _sealed()
    args = '{"command": "printf ' + token + '"}'
    assert json.loads(args)  # sanity: the wire form is valid JSON

    events = [
        b"data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_q",
                                    "type": "function",
                                    "function": {"name": "bash", "arguments": args},
                                }
                            ]
                        },
                    }
                ]
            }
        ).encode()
        + b"\n\n",
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    ]

    calls = _calls_from_frames(await _run(events))
    assert calls, "expected a tool call frame"
    assembled = "".join(c["function"]["arguments"] for c in calls)
    parsed = json.loads(assembled)  # invariant 1
    assert parsed["command"] == "printf " + _SECRET_PLAINTEXT  # invariant 2


@pytest.mark.asyncio
async def test_text_rescue_argument_json_parses_when_sealed_value_has_metachars():
    """The ``<gateway_tool_call>`` rescue path keeps ``arguments`` parseable."""
    token = _sealed()
    inner = {
        "id": "call_q",
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": '{"command": "printf ' + token + '"}',
        },
    }
    text = (
        "<gateway_tool_call>"
        + json.dumps(inner, ensure_ascii=False)
        + "</gateway_tool_call>"
    )

    events = [
        f'data: {json.dumps({"choices": [{"delta": {"content": text}}]})}\n\n'.encode(),
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    ]

    calls = _calls_from_frames(await _run(events))
    assert calls, "expected a tool call frame"
    assembled = "".join(c["function"]["arguments"] for c in calls)
    parsed = json.loads(assembled)
    assert parsed["command"] == "printf " + _SECRET_PLAINTEXT
