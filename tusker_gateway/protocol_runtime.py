"""Runtime lookup for verified protocol features.

Feature use is opt-in and fails closed if persistence is unavailable. This
module does not make external requests or expose endpoint credentials.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import lru_cache
from typing import Any

from tusker_gateway.protocol_compat import verified_protocol_features
from tusker_gateway.protocol_evidence import ProtocolEvidenceStore

logger = logging.getLogger(__name__)


def endpoint_identity(endpoint: dict[str, Any]) -> str:
    """Fingerprint a configured endpoint without including secrets.

    A deployment's base URL and chat path distinguish routed model aliases.
    Credentials and authentication headers are deliberately excluded.
    """
    value = {
        "base_url": str(endpoint.get("base_url") or "").rstrip("/").lower(),
        "chat_path": str(endpoint.get("chat_path") or ""),
        "provider": str(endpoint.get("provider") or ""),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]


@lru_cache(maxsize=1)
def _store() -> ProtocolEvidenceStore:
    return ProtocolEvidenceStore()


def verified_features_for_endpoint(
    provider: str, model: str, endpoint: dict[str, Any],
) -> frozenset[str]:
    """Return only fresh evidence; default off until operators opt in."""
    if os.getenv("TUSKER_PROTOCOL_EVIDENCE_ENABLED", "").lower() not in {
        "1", "true", "yes", "on",
    }:
        return frozenset()
    try:
        return verified_protocol_features(
            provider, endpoint_identity(endpoint), model, store=_store(),
        )
    except Exception:
        logger.warning(
            "protocol evidence lookup unavailable provider=%s model=%s",
            provider, model, exc_info=True,
        )
        return frozenset()
