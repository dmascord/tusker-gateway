"""Regression: an exact-cache hit must restore tokens before client delivery.

The cache stores the tokenized body (never plaintext). The hit path used to
`return web.json_response(hit)` verbatim, so the client received raw
[TUSKER:...] tokens instead of the values it originally sent.
"""
import asyncio
import json
from tusker_gateway.redaction import Tokenizer, RedactionRuleSet, restore_response

KEY = bytes.fromhex("ab" * 32)
SECRET_EMAIL = "cacheprobe" + chr(64) + "example-observer.test"


def test_cached_body_restores_before_client_response():
    t = Tokenizer(key=KEY, rules=RedactionRuleSet())
    cached = {
        "id": "c",
        "choices": [{"message": {"role": "assistant", "content": "mail " + t.tokenize_text(SECRET_EMAIL)}}],
    }
    # what the pre-fix code returned to the client:
    assert "[TUSKER:email:" in json.dumps(cached)
    # what the fixed path must return:
    restored = restore_response(cached, t)
    assert SECRET_EMAIL in json.dumps(restored)
    assert "[TUSKER" not in json.dumps(restored)
