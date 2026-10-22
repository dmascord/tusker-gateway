"""Format-preserving authenticated redaction regression tests."""
from __future__ import annotations

import json
import re

import pytest

from tusker_gateway.redaction import RedactionRuleSet, Tokenizer, _BufferedRestorer

KEY = b"0123456789abcdef0123456789abcdef"
EMAIL = "mail" + chr(64) + "example.test"




def tokenizer() -> Tokenizer:
    return Tokenizer(KEY, RedactionRuleSet(format_preserving_aliases=True))


@pytest.mark.parametrize("original", [EMAIL, "other" + chr(64) + "example.test"])
def test_email_alias_is_valid_stable_and_reversible(original: str) -> None:
    t = tokenizer()
    alias = t.tokenize_text(original)
    assert alias != original
    assert alias.endswith(".redact.invalid")
    assert re.fullmatch(r"[^@\s]+@[^@\s]+", alias)
    assert t.tokenize_text(original) == alias
    assert t.tokenize_text(alias) == alias
    assert t.restore_text(alias) == original


@pytest.mark.parametrize(
    ("prefix", "body"),
    [
        ("sk-", "a" * 35),
        ("ghp_", "b" * 35),
        ("gho_", "c" * 35),
        ("github_pat_", "d" * 35),
        ("npm_", "e" * 35),
        ("xoxb-", "f" * 35),
        ("AIza", "g" * 35),
    ],
)
def test_key_alias_preserves_prefix_and_restores(prefix: str, body: str) -> None:
    t = tokenizer()
    original = prefix + body
    alias = t.tokenize_text(original)
    assert alias != original
    assert alias.startswith(prefix + "z")
    assert t.tokenize_text(alias) == alias
    assert t.restore_text(alias) == original


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 19, 4096])
def test_streamed_alias_round_trip(chunk: int) -> None:
    t = tokenizer()
    original = "Contact " + EMAIL + " using " + "sk-" + "a" * 35 + " now!"
    wire = t.tokenize_text(original)
    restorer = _BufferedRestorer(t)
    actual = "".join(
        restorer.feed(wire[index : index + chunk])
        for index in range(0, len(wire), chunk)
    ) + restorer.flush()
    assert actual == original


def test_wrong_key_and_modified_alias_do_not_restore() -> None:
    t = tokenizer()
    alias = t.tokenize_text(EMAIL)
    other = Tokenizer(b"x" * 32, RedactionRuleSet(format_preserving_aliases=True))
    assert other.restore_text(alias) == alias
    mutated = alias.replace("u@", "x@")
    assert t.restore_text(mutated) == mutated


def test_legacy_mode_remains_default() -> None:
    t = Tokenizer(KEY, RedactionRuleSet())
    assert t.tokenize_text(EMAIL).startswith("[TUSKER:email:")


def test_mixed_old_and_new_tokens_restore() -> None:
    old = Tokenizer(KEY, RedactionRuleSet())
    new = tokenizer()
    old_token = old.tokenize_text(EMAIL)
    new_alias = new.tokenize_text(EMAIL)
    assert new.restore_text(old_token + " " + new_alias) == EMAIL + " " + EMAIL


@pytest.mark.asyncio
async def test_omp_tool_arguments_restore_aliases_without_encrypted_token_leakage() -> None:
    from tusker_gateway.endpoints import _normalize_stream

    t = tokenizer()
    alias = t.tokenize_text(EMAIL)
    args = json.dumps({"to": alias})

    async def upstream():
        yield (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "send_mail",
                                            "arguments": args,
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
            + "\n\n"
        ).encode()
        yield b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'

    frames = [
        frame
        async for frame in _normalize_stream(
            upstream(), provider="p", model="m", tools_requested=True, restorer=t
        )
    ]
    payload = b"".join(frames).decode()
    assert alias not in payload
    assert "[TUSKER:email:" not in payload
    assembled = "".join(
        call["function"]["arguments"]
        for frame in frames
        if frame.startswith(b"data: ")
        for call in (
            json.loads(frame[6:]).get("choices", [{}])[0]
            .get("delta", {})
            .get("tool_calls")
            or []
        )
    )
    assert json.loads(assembled)["to"] == EMAIL
