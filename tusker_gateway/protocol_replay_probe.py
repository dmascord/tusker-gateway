"""Opt-in, bounded multi-turn reasoning/tool replay contract probe.

This is a genuine two-request check, not an inference from model names.
The caller binds the authenticated upstream endpoint and must not retry
against a different model. Never store model-generated reasoning or tool data.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_probe import ProbeAssessment

ChatExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_ECHO = {
    "type": "function",
    "function": {
        "name": "protocol_probe_echo",
        "description": "Echo a short value",
        "parameters": {"type": "object", "properties": {
            "value": {"type": "string"}}, "required": ["value"],
            "additionalProperties": False},
    },
}


def _first_message(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    msg = choices[0].get("message")
    return msg if isinstance(msg, dict) else None


async def probe_reasoning_replay(
    *, provider: str, endpoint_id: str, model: str,
    execute: ChatExecutor, store: ProtocolEvidenceStore,
    timeout_seconds: float = 15.0,
) -> ProbeAssessment:
    """Prove native reasoning replay with a tool round-trip.

    Requires a nonempty upstream reasoning_content and an actual native
    tool call in the first turn. The follow-up replays the exact original
    assistant message and matching tool result. A successful follow-up
    proves acceptance, not the quality of the model's reasoning.
    """
    if not (0 < timeout_seconds <= 60):
        raise ValueError("timeout must be between 0 and 60 seconds")
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": "Call protocol_probe_echo with value 'ok', then confirm the result.",
    }]
    first_request = {
        "model": model, "messages": messages, "stream": False,
        "tools": [_ECHO], "tool_choice": "required", "max_tokens": 256,
    }
    try:
        first = await asyncio.wait_for(execute(first_request), timeout_seconds)
        assistant = _first_message(first)
        if assistant is None:
            result = ProbeAssessment("reasoning_replay", "unknown", "invalid_first_turn")
        else:
            reasoning = assistant.get("reasoning_content")
            calls = assistant.get("tool_calls")
            if not isinstance(reasoning, str) or not reasoning.strip():
                result = ProbeAssessment("reasoning_replay", "unknown", "no_reasoning_channel")
            elif not isinstance(calls, list) or len(calls) != 1:
                result = ProbeAssessment("reasoning_replay", "unknown", "no_single_native_call")
            else:
                call = calls[0]
                function = call.get("function") if isinstance(call, dict) else None
                call_id = call.get("id") if isinstance(call, dict) else None
                if not isinstance(call_id, str) or not call_id or not isinstance(function, dict) or function.get("name") != "protocol_probe_echo" or not _matches_probe(function.get("arguments")):
                    result = ProbeAssessment("reasoning_replay", "unknown", "invalid_call")
                else:
                    # Preserve all provider-supplied replay fields, including
                    # signatures, without persisting them in the evidence DB.
                    continuation = {
                        "model": model, "stream": False,
                        "messages": [*messages, dict(assistant), {
                            "role": "tool", "tool_call_id": call_id,
                            "content": '{"value":"ok"}',
                        }],
                        "max_tokens": 128,
                    }
                    second = await asyncio.wait_for(execute(continuation), timeout_seconds)
                    followup = _first_message(second)
                    result = ProbeAssessment(
                        "reasoning_replay",
                        "passed" if isinstance(followup, dict)
                        and isinstance(followup.get("content"), str)
                        and bool(followup["content"].strip()) else "unknown",
                        None if isinstance(followup, dict) and followup.get("content") else "invalid_continuation",
                    )
    except (asyncio.TimeoutError, ConnectionError, OSError):
        result = ProbeAssessment("reasoning_replay", "unavailable", "transport")
    store.record(provider, endpoint_id, model, "reasoning_replay", result.status)
    return result


def _matches_probe(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get('value') == 'ok'
