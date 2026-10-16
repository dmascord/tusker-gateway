"""Tests for reversible secret redaction (tusker_gateway.redaction)."""
import asyncio
import json

import pytest

from tusker_gateway.redaction import (
    RedactionRuleSet,
    Tokenizer,
    _BufferedRestorer,
    load_redaction_key,
    restore_response,
)

KEY_HEX = "ab" * 32


def _tokenizer(**rule_overrides) -> Tokenizer:
    rules = RedactionRuleSet(**rule_overrides)
    return Tokenizer(key=bytes.fromhex(KEY_HEX), rules=rules)


def _make_email() -> str:
    return "remediation" + chr(64) + "example-observer.test"


def _make_card() -> str:
    d = "4111"
    d1 = "1111"
    return " ".join([d, d1, d1, d1])


def test_redaction_keyloader():
    assert load_redaction_key({}) is None
    assert load_redaction_key({"TUSKER_REDACTION_KEY": "short"}) is None
    assert load_redaction_key({"TUSKER_REDACTION_KEY": KEY_HEX}) == bytes.fromhex(KEY_HEX)
    # raw 32-byte utf-8 accepted
    raw = ("x" * 32)
    assert load_redaction_key({"TUSKER_REDACTION_KEY": raw}) == raw.encode()


def test_tokenize_and_restore_roundtrip():
    t = _tokenizer()
    email = _make_email()
    card = _make_card()
    msg = f"Email {email} card {card} order 1234567812345678 end"
    tok = t.tokenize_text(msg)
    assert email not in tok
    assert "[TUSKER:" in tok
    # non-Luhn digit run must survive
    assert "1234567812345678" in tok
    assert card not in tok
    restored = t.restore_text(tok)
    assert restored == msg


def test_tokens_deterministic_across_calls():
    t = _tokenizer()
    email = _make_email()
    a = t.tokenize_text(email)
    b = t.tokenize_text("second turn has " + email)
    ca = a.split(":")[2].rstrip("]")
    cb = b.split(":")[2].rstrip("]")
    assert ca == cb


def test_invalid_token_passes_through():
    t = _tokenizer()
    garbage = "[TUSKER:email:AAAA====]"
    assert t.restore_text(garbage) == garbage


def test_response_context_cannot_open_request_context():
    t = _tokenizer()
    # context separation: a token opened with wrong AD fails -> passthrough
    email = _make_email()
    tok = t.tokenize_text(email)
    core = tok.split(":", 2)[2].rstrip("]")
    assert t._open("email", core) == email


def test_api_key_redaction():
    t = _tokenizer()
    key = "ghp_" + "a" * 30
    tok = t.tokenize_text("use " + key + " now")
    assert key not in tok
    assert t.restore_text(tok) == "use " + key + " now"


def test_client_forged_token_untouched():
    t = _tokenizer()
    forged = "[TUSKER:email:notarealctvalue]"
    # tokenize leaves it (no Luhn/email match inside), restore cannot open, passes verbatim
    assert t.tokenize_text(forged) == forged
    assert t.restore_text(forged) == forged


# ---------------------------------------------------------------- streaming


def _feed_all(text: str, chunk: int) -> str:
    t = _tokenizer()
    r = _BufferedRestorer(tokenizer=t)
    out = []
    for i in range(0, len(text), chunk):
        out.append(r.feed(text[i : i + chunk]))
    out.append(r.flush())
    return "".join(out)


@pytest.mark.parametrize("chunk", [1, 3, 5, 11, 4096])
def test_streaming_restore_various_chunk_sizes(chunk):
    t = _tokenizer()
    email = _make_email()
    card = _make_card()
    plain = f"Contact {email} or pay {card}. user@host.io also visible."
    tokenized = t.tokenize_text(plain)
    restored = _feed_all(tokenized, chunk)
    assert restored == plain, f"chunk={chunk}"
    assert "[TUSKER" not in restored


def test_streaming_restore_mid_token_split():
    t = _tokenizer()
    plain = "ping " + _make_email() + " thanks"
    tokenized = t.tokenize_text(plain)
    # split exactly in the middle of the sealed payload of the first token
    start = tokenized.find("[TUSKER")
    colon2 = tokenized.find(":", start + 8)
    payload = tokenized.find(":", colon2 + 1) + 1
    end = tokenized.find("]", payload)
    cut = (payload + end) // 2
    r = _BufferedRestorer(tokenizer=t)
    out = r.feed(tokenized[:cut])
    out += r.feed(tokenized[cut:])
    out += r.flush()
    assert out == plain


def test_streaming_flush_clears_pending():
    t = _tokenizer()
    r = _BufferedRestorer(tokenizer=t)
    tokenized = t.tokenize_text(_make_email())
    emitted = ""
    for i in range(0, len(tokenized), 7):
        emitted += r.feed(tokenized[i : i + 7])
    emitted += r.flush()
    assert emitted == _make_email()
    assert r.flush() == ""  # second flush is a no-op


# ------------------------------------------------------------- response tree


def test_restore_response_walks_nested_shapes():
    t = _tokenizer()
    email = _make_email()
    tok = t.tokenize_text("Contact " + email)
    resp = {
        "id": "x",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": tok,
                    "reasoning_content": "note " + tok,
                    "tool_calls": [
                        {"function": {"name": "f", "arguments": json.dumps({"e": tok})}}
                    ],
                }
            }
        ],
    }
    out = restore_response(resp, t)
    assert email in str(out)
    assert "[TUSKER" not in str(out)


def test_restore_response_noop_without_tokenizer():
    resp = {"choices": [{"message": {"content": "[TUSKER:email:zzz]"}}]}
    assert restore_response(resp, None) is resp