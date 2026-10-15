"""Provider-scoped tool/reasoning capabilities.

These rules describe *native API endpoints*, not model weights. OpenRouter,
vLLM and other OpenAI-compatible hosts can expose different contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCapabilities:
    family: str = "unknown"
    reasoning_field: str | None = None
    native_thinking_default: bool = False
    supports_explicit_tool_choice_in_thinking: bool = True
    preserves_reasoning_replay: bool = False
    tool_protocol: str = "openai_chat"


def resolve_tool_capabilities(provider: str, model: str) -> ToolCapabilities:
    provider_name = provider.lower().replace("_", "-")
    model_name = model.lower()
    if provider_name == "deepseek" and model_name.startswith("deepseek-"):
        # Current native DeepSeek Chat Completions defaults to thinking
        # enabled. A request may explicitly disable thinking; the model
        # slug alone cannot determine effective thinking mode.
        return ToolCapabilities(
            family="deepseek",
            reasoning_field="reasoning_content",
            native_thinking_default=True,
            supports_explicit_tool_choice_in_thinking=False,
            preserves_reasoning_replay=True,
        )
    if provider_name == "google":
        return ToolCapabilities(family="gemini", tool_protocol="openai_chat")
    if provider_name in {"anthropic", "claude"}:
        return ToolCapabilities(family="claude", tool_protocol="anthropic_messages")
    if provider_name in {"openai", "codex"}:
        return ToolCapabilities(family="openai", tool_protocol="openai_chat")
    return ToolCapabilities()


def deepseek_thinking_enabled(body: dict[str, Any], capabilities: ToolCapabilities) -> bool:
    """Determine native thinking state without guessing from model names."""
    if capabilities.family != "deepseek":
        return False
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        kind = thinking.get("type")
        if kind == "disabled":
            return False
        if kind == "enabled":
            return True
    effort = body.get("reasoning_effort")
    if isinstance(effort, str):
        return effort.lower() != "none"
    return capabilities.native_thinking_default


def prepare_native_deepseek_chat(body: dict[str, Any], capabilities: ToolCapabilities) -> None:
    """Normalize only documented native DeepSeek Chat Completions quirks.

    Keep assistant reasoning_content and tool_calls byte-for-byte intact.
    Do not invent missing reasoning: the originating client must replay it.
    """
    if capabilities.family != "deepseek":
        return
    if not deepseek_thinking_enabled(body, capabilities):
        return
    choice = body.get("tool_choice")
    # The native thinking endpoint rejects required/named tool choice. 'none'
    # and 'auto' are supported, so preserve their semantics.
    if choice is not None and choice not in ("auto", "none"):
        raise ValueError("DeepSeek thinking mode does not support required or named tool_choice")
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        # Native Chat Completions expects a nullable assistant content field.
        # Preserve the caller's reasoning and structured tool call history.
        if "content" not in message:
            message["content"] = None
