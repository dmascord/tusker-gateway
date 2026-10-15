"""Provider capability and DeepSeek replay regression tests."""
import copy
import pytest

from tusker_gateway.model_capabilities import (
    deepseek_thinking_enabled,
    prepare_native_deepseek_chat,
    resolve_tool_capabilities,
)


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-preview"])
def test_native_deepseek_thinking_defaults(model):
    cap = resolve_tool_capabilities("deepseek", model)
    assert cap.family == "deepseek"
    assert cap.reasoning_field == "reasoning_content"
    assert deepseek_thinking_enabled({}, cap)


def test_explicit_non_thinking_mode():
    cap = resolve_tool_capabilities("deepseek", "deepseek-chat")
    assert not deepseek_thinking_enabled({"thinking": {"type": "disabled"}}, cap)
    assert deepseek_thinking_enabled({"thinking": {"type": "enabled"}}, cap)


@pytest.mark.parametrize("choice", ["required", {"type": "function", "function": {"name": "read"}}])
def test_thinking_rejects_unsupported_tool_choice(choice):
    cap = resolve_tool_capabilities("deepseek", "deepseek-chat")
    with pytest.raises(ValueError, match="tool_choice"):
        prepare_native_deepseek_chat({"messages": [], "tool_choice": choice}, cap)


@pytest.mark.parametrize("choice", ["auto", "none"])
def test_thinking_preserves_supported_tool_choice(choice):
    cap = resolve_tool_capabilities("deepseek", "deepseek-chat")
    body = {"messages": [], "tool_choice": choice}
    prepare_native_deepseek_chat(body, cap)
    assert body["tool_choice"] == choice


def test_non_thinking_keeps_required_tool_choice():
    cap = resolve_tool_capabilities("deepseek", "deepseek-chat")
    body = {"thinking": {"type": "disabled"}, "messages": [], "tool_choice": "required"}
    prepare_native_deepseek_chat(body, cap)
    assert body["tool_choice"] == "required"


def test_replay_preserves_reasoning_tool_calls_and_results():
    cap = resolve_tool_capabilities("deepseek", "deepseek-reasoner")
    messages = [
        {"role": "user", "content": "Read the file"},
        {"role": "assistant", "reasoning_content": "Need the file",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "read", "arguments": '{"path":"a"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    body = {"messages": copy.deepcopy(messages)}
    prepare_native_deepseek_chat(body, cap)
    assert body["messages"][1]["content"] is None
    assert body["messages"][1]["reasoning_content"] == "Need the file"
    assert body["messages"][1]["tool_calls"] == messages[1]["tool_calls"]
    assert body["messages"][2] == messages[2]


def test_non_deepseek_provider_is_untouched():
    for provider in ("openrouter", "google", "anthropic", "openai", "vllm"):
        cap = resolve_tool_capabilities(provider, "deepseek-reasoner")
        body = {"messages": [{"role": "assistant", "tool_calls": []}], "tool_choice": "required"}
        before = copy.deepcopy(body)
        prepare_native_deepseek_chat(body, cap)
        assert body == before


def test_other_provider_capability_families():
    assert resolve_tool_capabilities("google", "gemini-2.5-pro").family == "gemini"
    assert resolve_tool_capabilities("anthropic", "claude-sonnet").tool_protocol == "anthropic_messages"
    assert resolve_tool_capabilities("openai", "gpt-5").family == "openai"
    assert resolve_tool_capabilities("other", "my-model").family == "unknown"
