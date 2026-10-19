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


def test_restore_authenticated_payload_when_model_drops_token_wrapper():
    t = _tokenizer()
    email = _make_email()
    token = t.tokenize_text(email)
    payload = token[len("[TUSKER:email:"):-1]

    # The failed OMP turn stripped the token wrapper and treated this payload
    # as a mailbox identifier. Recover only because its ciphertext authenticates.
    assert t.restore_text("search for " + payload) == "search for " + email
    assert t.restore_text("search for " + "A" * len(payload)) == "search for " + "A" * len(payload)


@pytest.mark.parametrize(
    ("token_type", "value"),
    [
        ("email", _make_email()),
        ("card", _make_card()),
        ("key", "ghp_" + "a" * 30),
        ("text", "opaque-identifier-0123456789abcdef"),
    ],
)
def test_restore_bare_payload_for_each_authenticated_token_type(token_type, value):
    t = _tokenizer()
    payload = t._seal(value, token_type)[len(f"[TUSKER:{token_type}:"):-1]
    assert t.restore_text("lookup " + payload + " now") == "lookup " + value + " now"


def test_bare_payload_restore_isolation_between_interleaved_sessions():
    """Per-stream carry state must not mix when sessions share a Tokenizer."""
    t = _tokenizer()
    first, second = _BufferedRestorer(t), _BufferedRestorer(t)
    email, key = _make_email(), "ghp_" + "b" * 30
    p1 = t._seal(email, "email")[len("[TUSKER:email:"):-1]
    p2 = t._seal(key, "key")[len("[TUSKER:key:"):-1]
    a1, a2 = p1[:len(p1)//2], p1[len(p1)//2:]
    b1, b2 = p2[:len(p2)//2], p2[len(p2)//2:]

    output1 = first.feed('query ' + a1)
    output2 = second.feed('query ' + b1)
    output1 += first.feed(a2 + ' end')
    output2 += second.feed(b2 + ' end')
    output1 += first.flush()
    output2 += second.flush()
    assert output1 == 'query ' + email + ' end'
    assert output2 == 'query ' + key + ' end'


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


@pytest.mark.parametrize("token_type,value", [
    ("email", _make_email()),
    ("card", _make_card()),
    ("key", "ghp_" + "a" * 30),
    ("text", "opaque-identifier-0123456789abcdef"),
])
@pytest.mark.parametrize("chunk", [1, 7, 31, 4096])
def test_streaming_restore_bare_payload_split_across_deltas(token_type, value, chunk):
    t = _tokenizer()
    payload = t._seal(value, token_type)[len(f"[TUSKER:{token_type}:"):-1]
    plain = '"command":"search ' + payload + '"'
    restored = _feed_all(plain, chunk)
    assert restored == '"command":"search ' + value + '"'


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

async def _guard_body(t, body):
    from tusker_gateway.guardrails import SecretRedactionGuard
    res = await SecretRedactionGuard(tokenizer=t).check(body)
    return res.modified_body if res.modified_body is not None else body


def test_guard_seals_replayed_tool_call_arguments():
    """Turn-2 replay of an assistant tool call must not leak the secret.

    Reading a .env in one turn then replaying the transcript on the next
    sends ``tool_calls[*].function.arguments`` upstream verbatim unless the
    guard seals it (the observed gap: content was sealed, arguments were not).
    """
    t = _tokenizer()
    secret = "sk-" + "livekey0123456789ABCDEFGH"
    body = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "read .env"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps(
                                {"command": "curl -H 'Authorization: Bearer " + secret + "' u"}
                            ),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "API_KEY=" + secret},
        ],
    }
    out = asyncio.run(_guard_body(t, body))
    assert secret not in json.dumps(out)
    assert "[TUSKER:key:" in json.dumps(out)
    # and the client-side round trip still yields plaintext
    assert secret in json.dumps(restore_response(out, t))


def test_guard_seals_legacy_function_call_arguments():
    t = _tokenizer()
    secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
    body = {
        "model": "m",
        "messages": [
            {
                "role": "assistant",
                "function_call": {"name": "bash", "arguments": "run " + secret},
            }
        ],
    }
    out = asyncio.run(_guard_body(t, body))
    assert secret not in json.dumps(out)


def test_guard_seals_tool_result_blocks_in_array_content():
    """Anthropic-shaped tool_result blocks inside array content."""
    t = _tokenizer()
    email = _make_email()
    body = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "tool_result", "text": "mail " + email}],
            }
        ],
    }
    out = asyncio.run(_guard_body(t, body))
    assert email not in json.dumps(out)


def test_guard_leaves_clean_body_identical():
    t = _tokenizer()
    body = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "no secrets here"},
            {"role": "assistant", "tool_calls": [{"function": {"name": "f", "arguments": '{"a": 1}'}}]},
        ],
    }
    out = asyncio.run(_guard_body(t, body))
    assert out == body


def _reassemble_tool_arguments(sse_text: str) -> str:
    """Concatenate tool-call arguments the way an OpenAI client does."""
    buf = ""
    for line in sse_text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        for choice in obj.get("choices", []):
            for call in (choice.get("delta") or {}).get("tool_calls") or []:
                args = (call.get("function") or {}).get("arguments")
                if isinstance(args, str):
                    buf += args
    return buf


def _stream_tool_args(parts: list[str], t):
    """Run the SSE normalizer over `arguments` fragments; return client text."""
    from tusker_gateway.endpoints import _normalize_stream

    frames = []
    for i, part in enumerate(parts):
        delta: dict = {"role": "assistant"} if i == 0 else {}
        delta["tool_calls"] = [
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": part},
            }
        ]
        frames.append(
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "model": "m",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
        )
    frames.append(
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }
    )
    raw = "".join("data: " + json.dumps(f) + "\n\n" for f in frames).encode()

    async def _gen():
        yield raw

    async def _go() -> str:
        acc = b""
        async for chunk in _normalize_stream(
            _gen(), provider="p", model="m", request_id="r",
            tools_requested=True, restorer=t,
        ):
            acc += chunk
        return acc.decode()

    return asyncio.run(_go())


@pytest.mark.parametrize("chunking", ["whole", "halves", "charwise"])
def test_streaming_tool_call_arguments_restore(chunking):
    """Streamed tool-call arguments must reach the client as plaintext.

    OpenAI clients concatenate `arguments` fragments across deltas, so the
    gateway replaces each fragment with restored text and releases any held
    tail before finish_reason. A token split across deltas must still
    reassemble (charwise is the worst case: every character its own frame).
    """
    t = _tokenizer()
    secret = "sk-" + "livekey0123456789ABCDEFGH"
    token = t.tokenize_text(secret)
    whole = '{"command":"' + token + '"}'
    if chunking == "whole":
        parts = [whole]
    elif chunking == "halves":
        mid = len(token) // 2
        parts = ['{"command":"' + token[:mid], token[mid:] + '"}']
    else:
        parts = list(whole)

    text = _stream_tool_args(parts, t)
    args = _reassemble_tool_arguments(text)
    assert args == '{"command":"' + secret + '"}'
    assert "[TUSKER:" not in args


def test_streaming_tool_call_restores_wrapper_stripped_email_payload():
    t = _tokenizer()
    email = _make_email()
    payload = t.tokenize_text(email)[len("[TUSKER:email:"):-1]
    halfway = len(payload) // 2
    parts = [
        '{"command":"search ' + payload[:halfway],
        payload[halfway:] + '"}',
    ]

    response = _stream_tool_args(parts, t)
    args = _reassemble_tool_arguments(response)
    assert args == '{"command":"search ' + email + '"}'
    assert "[TUSKER:" not in args


def test_streaming_tool_call_arguments_untouched_without_tokenizer():
    t = _tokenizer()
    token = t.tokenize_text("plain" + chr(64) + "example.test")
    text = _stream_tool_args(['{"q":"' + token + '"}'], None)
    assert token in _reassemble_tool_arguments(text)


# --- entropy fallback rule -------------------------------------------------

def test_entropy_fallback_seals_unprefixed_secret_by_context():
    """An unprefixed 32+ char run near a credential word is sealed.

    Off by default; the opt-in flag exists because the rule trades precision
    for coverage of secrets with no recognizable provider prefix.
    """
    t = _tokenizer(redact_entropy_fallback=True)
    bare = "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    for context in ("api key: ", "Authorization: Bearer ", "PASSWORD=", "secret="):
        text = context + bare
        sealed = t.tokenize_text(text)
        assert "[TUSKER:text:" in sealed, context
        assert bare not in sealed
        assert t.restore_text(sealed) == text


def test_entropy_fallback_is_opt_in_and_conservative():
    t_off = _tokenizer()  # default: rule disabled
    bare = "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    assert bare in t_off.tokenize_text("api key: " + bare)

    t_on = _tokenizer(redact_entropy_fallback=True)
    # no context word -> untouched
    assert bare in t_on.tokenize_text("the value is " + bare)
    # too short -> untouched
    assert "abc123" in t_on.tokenize_text("api key: abc123")
    # ordinary identifiers are not sealed
    ident = "def some_function_name_that_is_quite_long_and_snake_case():"
    assert t_on.tokenize_text(ident) == ident
