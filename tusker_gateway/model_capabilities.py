"""Conservative endpoint/model capability hints.

Unknown endpoints retain existing passthrough behaviour. Never infer a
model's tool protocol solely from an arbitrary alias.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCapabilities:
    family: str = "unknown"
    supports_explicit_tool_choice_in_thinking: bool = True
    reasoning_field: str | None = None


def resolve_tool_capabilities(provider: str, model: str) -> ToolCapabilities:
    provider_name = provider.lower().replace("_", "-")
    model_name = model.lower()
    # Only apply DeepSeek restrictions to its own endpoint. OpenRouter,
    # vLLM and third-party adapters may implement different semantics.
    if provider_name == "deepseek" and (
        model_name.startswith("deepseek-reasoner")
        or model_name.startswith("deepseek-v4")
    ):
        return ToolCapabilities(
            family="deepseek-thinking",
            supports_explicit_tool_choice_in_thinking=False,
            reasoning_field="reasoning_content",
        )
    if provider_name == "deepseek":
        return ToolCapabilities(family="deepseek", reasoning_field="reasoning_content")
    return ToolCapabilities()
