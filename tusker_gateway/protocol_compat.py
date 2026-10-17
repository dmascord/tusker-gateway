"""Conservative model-protocol compatibility resolver.

The external Chat Completions contract is stable; upstream request adaptation
is scoped to the *actual* provider and model on each retry. Unknown models
pass through unchanged. This is intentionally separate from capability probes:
a model's ability to use tools does not identify its parameter dialect.

Operators can declare a forced ``passed`` (or ``unsupported``) verdict per
route via ``TUSKER_PROTOCOL_OVERRIDES_JSON`` for cases where a live probe
is undesirable but the operator has confirmed the dialect. Fresh
``passed`` evidence still wins; only an *absence* of fresh evidence
causes the override to be honoured.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_VALID_OVERRIDE_STATUSES = frozenset({"passed", "unsupported"})


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
        replay_reasoning=False,  # Never infer replay requirements from model names.
        thinking_parameter="thinking" if p in {"zai", "z.ai", "zhipu"} else None,
        supports_reasoning_effort=p in {"openai", "apim", "opencode-go"},
        confidence="provider" if native else "heuristic" if family != "unknown" else "unknown",
    )


def adapt_upstream_chat(
    body: dict[str, Any], provider: str, model: str, *,
    verified_features: frozenset[str] = frozenset(),
) -> ProtocolProfile:
    """Apply only safe, provider-scoped changes. Never discard reasoning.

    Unknown and third-party-hosted models are left untouched; the gateway's
    existing provider-specific transformations remain authoritative.
    """
    profile = resolve_protocol(provider, model)
    if "reasoning_replay" in verified_features and isinstance(body.get("messages"), list):
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

def _override_route_key(provider: str, model: str | None) -> str:
    p = (provider or "").strip().lower()
    if not model:
        return p
    m = model.strip().lower()
    return f"{p}/{m}" if p else m


def _load_overrides() -> dict[str, dict[str, str]]:
    """Parse ``TUSKER_PROTOCOL_OVERRIDES_JSON`` (read per call for hot reload).

    Format (provider and provider/model keys both supported; model keys win)::

        {"deepseek": {"reasoning_replay": "passed"},
         "zai/glm-4.6": {"thinking_parameter": "passed"}}
    """
    raw = os.environ.get("TUSKER_PROTOCOL_OVERRIDES_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("ignoring TUSKER_PROTOCOL_OVERRIDES_JSON: invalid JSON")
        return {}
    if not isinstance(data, dict):
        return {}
    parsed: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        per_feature: dict[str, str] = {}
        for feature, status in value.items():
            if isinstance(status, str) and status in _VALID_OVERRIDE_STATUSES:
                per_feature[str(feature)] = status
        if per_feature:
            parsed[_override_route_key(str(key), None)] = per_feature
    return parsed


def operator_overrides_for(provider: str, model: str) -> dict[str, str]:
    """Return the operator-declared verdicts for ``provider/model``."""
    overrides = _load_overrides()
    if not overrides:
        return {}
    route_key = _override_route_key(provider, model)
    per_feature = dict(overrides.get(route_key, {}))
    if "/" not in route_key:
        return per_feature
    provider_key = route_key.split("/", 1)[0]
    for feature, status in overrides.get(provider_key, {}).items():
        per_feature.setdefault(feature, status)
    return per_feature


def verified_protocol_features(
    provider: str, endpoint_id: str, model: str, *,
    store: Any,
) -> frozenset[str]:
    """Fresh, version-matched evidence; otherwise the operator override.

    An operator-declared ``passed`` verdict is honoured only when no fresh
    probe evidence exists. A probe re-recording ``unavailable`` (or a
    different status) for the same route wins over the override. This
    makes the override a fill-in for routes the operator has confirmed
    manually, not a way to silence a failing probe.
    """
    from tusker_gateway.protocol_evidence import PROTOCOL_FEATURES
    passed = set()
    overrides = _load_overrides()
    route_key = _override_route_key(provider, model)
    model_overrides = overrides.get(route_key, {}) if "/" in route_key else {}
    provider_overrides = overrides.get((provider or "").strip().lower(), {})
    for feature in PROTOCOL_FEATURES:
        evidence = store.get(provider, endpoint_id, model, feature)
        if evidence is not None and evidence.verified:
            passed.add(feature)
            continue
        if evidence is not None:
            # Fresh non-passed evidence (unavailable / unsupported / unknown)
            # outranks the override; honour the probe.
            continue
        declared = model_overrides.get(feature) or provider_overrides.get(feature)
        if declared == "passed":
            passed.add(feature)
    return frozenset(passed)


def resolve_with_evidence(
    provider: str, model: str, endpoint_id: str, *, store: Any,
) -> tuple[ProtocolProfile, frozenset[str]]:
    """Resolve a model family hint and separately verified capabilities."""
    return resolve_protocol(provider, model), verified_protocol_features(
        provider, endpoint_id, model, store=store,
    )
