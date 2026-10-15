"""Model-family capability guard regression tests."""
import pytest

from tusker_gateway.model_capabilities import resolve_tool_capabilities


@pytest.mark.parametrize("model", ["deepseek-reasoner", "deepseek-v4", "deepseek-v4-preview"])
def test_native_deepseek_thinking_restricts_tool_choice(model):
    cap = resolve_tool_capabilities("deepseek", model)
    assert cap.family == "deepseek-thinking"
    assert cap.supports_explicit_tool_choice_in_thinking is False
    assert cap.reasoning_field == "reasoning_content"


def test_other_deepseek_models_keep_existing_tool_choice():
    assert resolve_tool_capabilities("deepseek", "deepseek-chat").supports_explicit_tool_choice_in_thinking


@pytest.mark.parametrize("provider", ["openrouter", "vllm", "opencode-zen"])
def test_third_party_deepseek_names_do_not_inherit_native_restrictions(provider):
    assert resolve_tool_capabilities(provider, "deepseek-v4").supports_explicit_tool_choice_in_thinking


def test_unknown_model_is_conservative():
    assert resolve_tool_capabilities("custom", "my-alias").family == "unknown"
