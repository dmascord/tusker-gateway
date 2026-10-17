"""Provider replay nested-structure isolation tests."""
from tusker_gateway.protocol_signatures import (
    copy_replay_message, strip_cross_provider_replay,
)


def test_nested_signature_isolation():
    original = {"role": "assistant", "content": [{
        "type": "text", "text": "hello",
        "metadata": {"thought_signature": "opaque"},
    }]}
    replay = copy_replay_message(original)
    replay["content"][0]["metadata"]["thought_signature"] = "changed"
    assert original["content"][0]["metadata"]["thought_signature"] == "opaque"


def test_cross_provider_does_not_mutate_nested_original():
    original = {"role": "assistant", "content": [{
        "type": "thinking", "thinking": "secret",
        "signature": "signed",
    }, {"type": "text", "text": "hello"}]}
    mapped = strip_cross_provider_replay(original, same_upstream=False)
    assert mapped["content"] == [{"type": "text", "text": "hello"}]
    assert original["content"][0]["signature"] == "signed"
