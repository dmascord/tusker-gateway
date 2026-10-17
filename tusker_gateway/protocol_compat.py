"""Conservative model-protocol compatibility resolver.

The external Chat Completions contract is stable; upstream request adaptation
is scoped to the *actual* provider and model on each retry. Unknown models
pass through unchanged. This is intentionally separate from capability probes:
a model's ability to use tools does not identify its parameter dialect.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProtocolProfile:
    provider: str
    model: str
    family: str
    reasoning_field: str = "reasoning_content"
    replay_reasoning: bool = False
    thinking_parameter: str | None = None
    supports_reasoning_effort: bool = False
    confidence: str = "unknown"


def resolve_protocol(provider: str, model: str) -> ProtocolProfile:
    p, m = provider.lower().strip(), model.lower().strip()
    # Family hints are useful for diagnostics, but must not be treated as
    # proof that a third-party host implements the native API dialect.
    families = (
        ("deepseek", "deepseek"), ("glm-", "glm"), ("qwen", "qwen"),
        ("kimi", "kimi"), ("moonshot", "kimi"), ("minimax", "minimax"),
        ("mimo", "mimo"), ("gpt-", "openai"), ("claude", "claude"),
        ("gemini", "gemini"),
    )
    family = next((label for prefix, label in families if prefix in m), "unknown")
    native = (p in {"deepseek", "zai", "z.ai", "zhipu", "moonshot", "minimax"})
    return ProtocolProfile(
        provider=p, model=m, family=family,
        replay_reasoning=native and family in {"deepseek", "glm", "kimi"},
        thinking_parameter="thinking" if p in {"zai", "z.ai", "zhipu"} else None,
        supports_reasoning_effort=p in {"openai", "apim", "opencode-go"},
        confidence="provider" if native else "heuristic" if family != "unknown" else "unknown",
    )


def adapt_upstream_chat(body: dict[str, Any], provider: str, model: str) -> ProtocolProfile:
    """Apply only safe, provider-scoped changes. Never discard reasoning.

    Unknown and third-party-hosted models are left untouched; the gateway's
    existing provider-specific transformations remain authoritative.
    """
    profile = resolve_protocol(provider, model)
    if profile.replay_reasoning:
        # Retain original assistant reasoning_content on continuation turns.
        # Do not synthesize absent thinking or duplicate it into content.
        for message in body.get("messages", []):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            if message.get("reasoning_content") is None and isinstance(message.get("reasoning"), str):
                message["reasoning_content"] = message["reasoning"]
    return profile
