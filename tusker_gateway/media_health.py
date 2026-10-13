"""Request-time health checks for qualified embedding and rerank routes."""
from __future__ import annotations

import os
import time
from typing import Any

from tusker_gateway.model_capability import ModelCapabilityDB

MEDIA_CAPABILITY_PROBE_VERSION = "media-capability-v1"
EMBEDDING_CAPABILITY = "embedding"
RERANK_CAPABILITY = "rerank"
_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})


def media_capability_gate_enabled() -> bool:
    return os.environ.get("TUSKER_MEDIA_CAPABILITY_GATE_ENABLED", "false").strip().lower() in _ENABLED_VALUES


def media_capability_allowed(
    capability_db: ModelCapabilityDB | None,
    *,
    provider: str,
    model: str,
    capability: str,
) -> bool:
    """Allow routes unless fresh, current evidence explicitly says unsupported."""
    if not media_capability_gate_enabled() or capability_db is None:
        return True
    evidence = capability_db.get(provider, model, capability)
    if evidence is None or evidence.probe_version != MEDIA_CAPABILITY_PROBE_VERSION:
        return True
    try:
        max_age = max(
            1.0,
            float(os.environ.get("TUSKER_MEDIA_CAPABILITY_MAX_AGE_SECS", "86400")),
        )
    except (TypeError, ValueError):
        max_age = 86400.0
    if time.time() - evidence.checked_at > max_age:
        return True
    return evidence.status != "unsupported"


__all__ = [
    "EMBEDDING_CAPABILITY",
    "MEDIA_CAPABILITY_PROBE_VERSION",
    "RERANK_CAPABILITY",
    "media_capability_allowed",
    "media_capability_gate_enabled",
]
