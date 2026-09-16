"""Unit tests for the Gemini thought_signature cache."""
from __future__ import annotations

import time

from tusker_gateway.gemini_thought import GeminiThoughtCache


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_tool_call(call_id: str, *, sig: str | None = None) -> dict:
    tc: dict = {
        "id": call_id,
        "type": "function",
        "function": {"name": "default_api:bash", "arguments": "{}"},
    }
    if sig:
        tc["extra_content"] = {"google": {"thought_signature": sig}}
    return tc


def _make_response(*tool_calls: dict) -> dict:
    return {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "tool_calls": list(tool_calls),
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

class TestExtract:
    def test_extracts_from_tool_calls(self):
        resp = _make_response(
            _make_tool_call("call_1", sig="sig_aaa"),
            _make_tool_call("call_2", sig="sig_bbb"),
        )
        found = GeminiThoughtCache.extract_from_response(resp)
        assert found == {"call_1": "sig_aaa", "call_2": "sig_bbb"}

    def test_extracts_from_streaming_delta(self):
        resp = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            _make_tool_call("call_99", sig="sig_delta"),
                        ]
                    }
                }
            ]
        }
        found = GeminiThoughtCache.extract_from_response(resp)
        assert found == {"call_99": "sig_delta"}

    def test_skips_missing_signature(self):
        resp = _make_response(_make_tool_call("call_3"))
        found = GeminiThoughtCache.extract_from_response(resp)
        assert found == {}

    def test_handles_malformed_response(self):
        assert GeminiThoughtCache.extract_from_response(None) == {}
        assert GeminiThoughtCache.extract_from_response({}) == {}
        assert GeminiThoughtCache.extract_from_response({"choices": [None]}) == {}


# ---------------------------------------------------------------------------
# store / get
# ---------------------------------------------------------------------------

class TestCacheStoreGet:
    def test_round_trip(self):
        c = GeminiThoughtCache(ttl_secs=10)
        c.store("call_1", "sig_abc")
        assert c.get("call_1") == "sig_abc"

    def test_returns_none_for_missing(self):
        c = GeminiThoughtCache()
        assert c.get("nope") is None

    def test_returns_none_after_ttl(self):
        c = GeminiThoughtCache(ttl_secs=0.01)
        c.store("call_1", "sig_abc")
        time.sleep(0.02)
        assert c.get("call_1") is None

    def test_store_many(self):
        c = GeminiThoughtCache(ttl_secs=10)
        c.store_many({"call_a": "sig_a", "call_b": "sig_b"})
        assert c.get("call_a") == "sig_a"
        assert c.get("call_b") == "sig_b"

    def test_ignores_empty_key(self):
        c = GeminiThoughtCache()
        c.store("", "sig")
        assert c.get("") is None

    def test_ignores_empty_signature(self):
        c = GeminiThoughtCache()
        c.store("call_1", "")
        assert c.get("call_1") is None

    def test_lru_eviction(self):
        c = GeminiThoughtCache(max_entries=2, ttl_secs=60)
        c.store("a", "1")
        c.store("b", "2")
        c.store("c", "3")
        assert c.get("a") is None  # evicted
        assert c.get("b") == "2"
        assert c.get("c") == "3"


# ---------------------------------------------------------------------------
# extract_and_store
# ---------------------------------------------------------------------------

class TestExtractAndStore:
    def test_stores_new_entries(self):
        c = GeminiThoughtCache(ttl_secs=10)
        resp = _make_response(_make_tool_call("call_x", sig="sig_x"))
        n = c.extract_and_store(resp)
        assert n == 1
        assert c.get("call_x") == "sig_x"

    def test_returns_zero_for_no_sigs(self):
        c = GeminiThoughtCache()
        resp = _make_response(_make_tool_call("call_y"))
        assert c.extract_and_store(resp) == 0


# ---------------------------------------------------------------------------
# inject_into_messages
# ---------------------------------------------------------------------------

class TestInjectIntoMessages:
    def test_injects_missing_signature(self):
        c = GeminiThoughtCache(ttl_secs=10)
        c.store("call_1", "sig_from_cache")

        messages = [
            {
                "role": "assistant",
                "tool_calls": [_make_tool_call("call_1")],  # no sig
            }
        ]
        patched = c.inject_into_messages(messages)
        assert patched == 1
        sig = (
            messages[0]["tool_calls"][0]
            .get("extra_content", {})
            .get("google", {})
            .get("thought_signature")
        )
        assert sig == "sig_from_cache"

    def test_skips_when_signature_already_present(self):
        c = GeminiThoughtCache(ttl_secs=10)
        c.store("call_1", "cached_sig")

        messages = [
            {
                "role": "assistant",
                "tool_calls": [_make_tool_call("call_1", sig="original_sig")],
            }
        ]
        patched = c.inject_into_messages(messages)
        assert patched == 0
        sig = (
            messages[0]["tool_calls"][0]
            .get("extra_content", {})
            .get("google", {})
            .get("thought_signature")
        )
        assert sig == "original_sig"  # untouched

    def test_skips_non_assistant_messages(self):
        c = GeminiThoughtCache(ttl_secs=10)
        c.store("call_1", "sig")
        messages = [{"role": "user", "content": "hi"}]
        assert c.inject_into_messages(messages) == 0

    def test_skips_tool_messages(self):
        c = GeminiThoughtCache(ttl_secs=10)
        c.store("call_1", "sig")
        messages = [{"role": "tool", "tool_call_id": "call_1", "content": "ok"}]
        assert c.inject_into_messages(messages) == 0

    def test_handles_empty_messages(self):
        c = GeminiThoughtCache()
        assert c.inject_into_messages([]) == 0
        assert c.inject_into_messages(None) == 0  # type: ignore[arg-type]

    def test_multiple_tool_calls_same_message(self):
        c = GeminiThoughtCache(ttl_secs=10)
        c.store("call_a", "sig_a")
        c.store("call_b", "sig_b")

        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    _make_tool_call("call_a"),  # missing
                    _make_tool_call("call_b"),  # missing
                ],
            }
        ]
        patched = c.inject_into_messages(messages)
        assert patched == 2


# ---------------------------------------------------------------------------
# round-trip: extract → store → inject
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_full_lifecycle(self):
        """Simulate: Gemini response → client strips sig → next request gets it injected."""
        cache = GeminiThoughtCache(ttl_secs=60)

        # Step 1: Gemini returns tool_calls with signatures.
        gemini_response = _make_response(
            _make_tool_call("call_101", sig="enEKbaaRealSignature"),
        )
        cache.extract_and_store(gemini_response)
        assert cache.get("call_101") == "enEKbaaRealSignature"

        # Step 2: Client echoes back the assistant message WITHOUT signature.
        client_messages = [
            {"role": "user", "content": "do something"},
            {
                "role": "assistant",
                "tool_calls": [_make_tool_call("call_101")],  # sig stripped
            },
            {"role": "tool", "tool_call_id": "call_101", "content": "done"},
            {"role": "user", "content": "now what?"},
        ]

        # Step 3: Gateway injects the cached signature before sending to Gemini.
        patched = cache.inject_into_messages(client_messages)
        assert patched == 1

        # Step 4: The tool_call now carries the original signature.
        sig = (
            client_messages[1]["tool_calls"][0]
            .get("extra_content", {})
            .get("google", {})
            .get("thought_signature")
        )
        assert sig == "enEKbaaRealSignature"
