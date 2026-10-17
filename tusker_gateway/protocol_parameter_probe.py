"""Specialised, bounded request-parameter compatibility probes.

A successful generic completion is not sufficient. Probe a documented
parameter on a bound upstream, then verify the response envelope. The
caller controls credentials, endpoint, cost and permission to probe.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_probe import ProbeAssessment

Executor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_ALLOWED = {
    "reasoning_effort": {"minimal", "low", "medium", "high"},
    "thinking_parameter": {"enabled", "disabled"},
}


async def probe_parameter(
    *, provider: str, endpoint_id: str, model: str,
    feature: str, execute: Executor, store: ProtocolEvidenceStore,
    timeout_seconds: float = 15.0,
) -> ProbeAssessment:
    """Check accepted syntax, not whether the model actually reasons.

    Parameter mapping is native-provider-specific. Never send an unknown
    dialect automatically to an unverified third-party host.
    """
    if feature not in _ALLOWED or not (0 < timeout_seconds <= 60):
        raise ValueError("unsupported parameter probe")
    request: dict[str, Any] = {
        "model": model, "stream": False, "max_tokens": 64,
        "messages": [{"role": "user", "content": "Reply with the word ok."}],
    }
    if feature == "reasoning_effort":
        request["reasoning_effort"] = "low"
    else:
        request["thinking"] = {"type": "enabled"}
    try:
        response = await asyncio.wait_for(execute(request), timeout_seconds)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        result = ProbeAssessment(feature, "unavailable", "transport")
    else:
        if not isinstance(response, dict) or not isinstance(response.get("choices"), list) or not response["choices"]:
            result = ProbeAssessment(feature, "unknown", "invalid_envelope")
        elif not isinstance(response["choices"][0], dict) or not isinstance(response["choices"][0].get("message"), dict):
            result = ProbeAssessment(feature, "unknown", "invalid_message")
        else:
            # Some proxies silently ignore unknown parameters. Without
            # provider-specific confirmation, acceptance is inconclusive.
            result = ProbeAssessment(feature, "unknown", "parameter_may_be_ignored")
    store.record(provider, endpoint_id, model, feature, result.status)
    return result
