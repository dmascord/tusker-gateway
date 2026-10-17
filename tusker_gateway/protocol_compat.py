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
    if profile.replay_reasoning and isinstance(body.get("messages"), list):
        # Copy-on-write: candidate failover must not mutate the shared
        # conversation history for a different provider's replay dialect.
        changed = False
        mapped = []
        for message in body["messages"]:
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and message.get("reasoning_content") is None
                and isinstance(message.get("reasoning"), str)
            ):
                mapped.append({**message, "reasoning_content": message["reasoning"]})
                changed = True
            else:
                mapped.append(message)
        if changed:
            body["messages"] = mapped
    return profile


def verified_protocol_features(
    provider: str, endpoint_id: str, model: str, *,
    store: Any,
) -> frozenset[str]:
    """Only fresh, version-matched probe evidence may enable features."""
    from tusker_gateway.protocol_evidence import PROTOCOL_FEATURES
    passed = set()
    for feature in PROTOCOL_FEATURES:
        evidence = store.get(provider, endpoint_id, model, feature)
        if evidence is not None and evidence.verified:
            passed.add(feature)
    return frozenset(passed)


def resolve_with_evidence(
    provider: str, model: str, endpoint_id: str, *, store: Any,
) -> tuple[ProtocolProfile, frozenset[str]]:
    """Resolve a model family hint and separately verified capabilities."""
    return resolve_protocol(provider, model), verified_protocol_features(
        provider, endpoint_id, model, store=store,
    )
