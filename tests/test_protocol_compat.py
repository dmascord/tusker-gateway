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
