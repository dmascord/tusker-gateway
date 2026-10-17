"""Model protocol compatibility contract tests (not executed)."""
from tusker_gateway.protocol_compat import adapt_upstream_chat, resolve_protocol


def test_native_deepseek_replays_reasoning_without_promoting():
    body = {"messages": [{"role": "assistant", "content": None,
                          "reasoning": "internal", "tool_calls": [{"id": "c1"}]}]}
    profile = adapt_upstream_chat(body, "deepseek", "deepseek-chat", verified_features=frozenset({"reasoning_replay"}))
    assert not profile.replay_reasoning  # family hint is not evidence
    assert body["messages"][0]["reasoning_content"] == "internal"
    assert body["messages"][0]["content"] is None


def test_third_party_host_does_not_invent_native_dialect():
    body = {"messages": [{"role": "assistant", "reasoning": "thought"}]}
    profile = adapt_upstream_chat(body, "openrouter", "deepseek-r1")
    assert profile.family == "deepseek"
    assert profile.confidence == "heuristic"
    assert body["messages"][0].get("reasoning_content") is None


def test_glm_native_provider_profile():
    profile = resolve_protocol("zai", "glm-4.7")
    assert profile.family == "glm"
    assert profile.thinking_parameter == "thinking"


def test_unknown_model_passthrough():
    body = {"messages": [{"role": "user", "content": "hello"}]}
    before = repr(body)
    profile = adapt_upstream_chat(body, "custom-host", "unknown-123")
    assert profile.confidence == "unknown"
    assert repr(body) == before


def test_qwen_and_kimi_family_detection():
    assert resolve_protocol("openrouter", "qwen3-coder").family == "qwen"
    assert resolve_protocol("moonshot", "kimi-k2").family == "kimi"

def test_operator_override_grants_passed_when_no_evidence(monkeypatch):
    from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
    from tusker_gateway.protocol_compat import verified_protocol_features

    monkeypatch.setenv(
        "TUSKER_PROTOCOL_OVERRIDES_JSON",
        '{"deepseek": {"reasoning_replay": "passed"},'
        ' "deepseek/deepseek-r1": {"structured_tool_calls": "passed"}}',
    )
    store = ProtocolEvidenceStore(":memory:")
    passed = verified_protocol_features(
        "deepseek", "endpoint-x", "deepseek-r1", store=store,
    )
    assert "reasoning_replay" in passed
    assert "structured_tool_calls" in passed


def test_fresh_probe_evidence_outranks_override(monkeypatch):
    from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
    from tusker_gateway.protocol_compat import verified_protocol_features

    monkeypatch.setenv(
        "TUSKER_PROTOCOL_OVERRIDES_JSON",
        '{"deepseek": {"reasoning_replay": "passed"}}',
    )
    store = ProtocolEvidenceStore(":memory:")
    store.record(
        "deepseek", "endpoint-x", "deepseek-r1",
        "reasoning_replay", "unavailable",
    )
    assert "reasoning_replay" not in verified_protocol_features(
        "deepseek", "endpoint-x", "deepseek-r1", store=store,
    )


def test_unsupported_override_is_not_a_pass(monkeypatch):
    from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
    from tusker_gateway.protocol_compat import verified_protocol_features

    monkeypatch.setenv(
        "TUSKER_PROTOCOL_OVERRIDES_JSON",
        '{"custom-host": {"structured_tool_calls": "unsupported"}}',
    )
    store = ProtocolEvidenceStore(":memory:")
    assert verified_protocol_features(
        "custom-host", "endpoint-x", "m", store=store,
    ) == frozenset()


def test_invalid_override_is_ignored(monkeypatch):
    from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
    from tusker_gateway.protocol_compat import verified_protocol_features

    monkeypatch.setenv(
        "TUSKER_PROTOCOL_OVERRIDES_JSON",
        '{"deepseek": {"structured_tool_calls": "banana"}}',
    )
    store = ProtocolEvidenceStore(":memory:")
    assert verified_protocol_features(
        "deepseek", "endpoint-x", "deepseek-r1", store=store,
    ) == frozenset()
