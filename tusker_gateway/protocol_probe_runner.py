"""Bounded, opt-in protocol probe orchestration.

The caller supplies an authenticated, provider-specific async chat executor.
This runner does not discover credentials, select endpoints or silently send
extra billable requests. It stores only classified feature/status evidence.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_probe import ProbeAssessment, classify_probe

ProbeExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_TOOL = {
    "type": "function",
    "function": {
        "name": "protocol_probe_echo",
        "description": "Return a short probe string.",
        "parameters": {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        },
    },
}


async def probe_chat_contract(
    *, provider: str, endpoint_id: str, model: str,
    execute: ProbeExecutor, store: ProtocolEvidenceStore,
    timeout_seconds: float = 15.0,
) -> dict[str, ProbeAssessment]:
    """Execute one bounded chat/tool probe and record conservative evidence.

    Call this only after explicit operator opt-in; the executor must bind the
    exact endpoint/model and must not perform pool fallback. No raw payloads
    are retained. Reasoning replay and other multi-turn features are left
    unknown until specialized probes exist.
    """
    if not (0 < timeout_seconds <= 60):
        raise ValueError("timeout must be between 0 and 60 seconds")
    request = {
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": (
            "Call protocol_probe_echo with value 'ok'. Do not answer in prose."
        )}],
        "tools": [_TOOL], "tool_choice": "required",
        "max_tokens": 128,
    }
    try:
        response = await asyncio.wait_for(execute(request), timeout=timeout_seconds)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        assessments = {
            feature: ProbeAssessment(feature, "unavailable", "transport")
            for feature in ("structured_tool_calls", "tool_call_finish", "reasoning_channel")
        }
    except Exception:
        # Do not mask arbitrary programmer errors as unsupported model
        # capabilities. The caller decides whether to retry/report.
        raise
    else:
        assessments = {
            feature: classify_probe(feature, response=response)
            for feature in ("structured_tool_calls", "tool_call_finish", "reasoning_channel")
        }
    for feature, result in assessments.items():
        store.record(provider, endpoint_id, model, feature, result.status)
    return assessments
