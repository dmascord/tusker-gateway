"""Provider signature preservation and isolation fixtures."""
from tusker_gateway.protocol_signatures import (
    copy_replay_message, strip_cross_provider_replay,
)


def test_preserve_native_signature_without_mutating_source():
    source = {"role": "assistant", "reasoning_content": "private",
              "tool_calls": [{"id": "c1", "thought_signature": "opaque"}]}
    replay = copy_replay_message(source)
    replay["tool_calls"][0]["id"] = "changed"
    assert source["tool_calls"][0]["id"] == "c1"
    assert replay["tool_calls"][0]["thought_signature"] == "opaque"


def test_strip_opaque_cross_provider_state():
    source = {"role": "assistant", "content": [
        {"type": "thinking", "signature": "signed", "thinking": "private"},
        {"type": "text", "text": "hello"},
    ], "reasoning_content": "private", "thought_signature": "opaque"}
    mapped = strip_cross_provider_replay(source, same_upstream=False)
    assert mapped["content"] == [{"type": "text", "text": "hello"}]
    assert "reasoning_content" not in mapped
    assert "thought_signature" not in mapped
    assert source["reasoning_content"] == "private"


def test_same_upstream_preserves_reasoning():
    source = {"role": "assistant", "reasoning_content": "private",
              "thought_signature": "opaque"}
    assert strip_cross_provider_replay(source, same_upstream=True) == source
