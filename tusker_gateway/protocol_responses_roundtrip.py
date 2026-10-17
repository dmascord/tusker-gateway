"""Bounded two-turn Responses API function-call/output probe.

The executor is bound by its caller to an exact authenticated endpoint/model.
Only a complete function-call round trip can establish support.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_probe import ProbeAssessment
from tusker_gateway.protocol_responses_probe import classify_responses_function_call

ResponsesExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def probe_responses_roundtrip(
    *, provider: str, endpoint_id: str, model: str,
    execute: ResponsesExecutor, store: ProtocolEvidenceStore,
    timeout_seconds: float = 15.0,
) -> ProbeAssessment:
    if not (0 < timeout_seconds <= 60):
        raise ValueError("invalid probe timeout")
    request = {
        "model": model,
        "input": [{"role": "user", "content": "Call protocol_probe_echo with value ok."}],
        "tools": [{
            "type": "function", "name": "protocol_probe_echo",
            "description": "Echo a short value",
            "parameters": {
                "type": "object", "properties": {"value": {"type": "string"}},
                "required": ["value"], "additionalProperties": False,
            },
        }],
        "tool_choice": "required",
        "max_output_tokens": 128,
    }
    try:
        first = await asyncio.wait_for(execute(request), timeout_seconds)
        initial = classify_responses_function_call(first)
        if initial.status != "passed":
            result = initial
        else:
            call = next(item for item in first["output"] if isinstance(item, dict)
                        and item.get("type") == "function_call"
                        and item.get("name") == "protocol_probe_echo"
                        and isinstance(item.get("call_id"), str) and item["call_id"]
                        and isinstance(item.get("arguments"), str)
                        and _is_expected_arguments(item["arguments"]))
            # Preserve the native output item and call_id; do not invent
            # response IDs or mutate the upstream model's tool call.
            continuation = {
                "model": model,
                "input": [
                    *request["input"], *first["output"],
                    {"type": "function_call_output", "call_id": call["call_id"],
                     "output": '{"value":"ok"}'},
                ],
                "max_output_tokens": 128,
            }
            second = await asyncio.wait_for(execute(continuation), timeout_seconds)
            output = second.get("output") if isinstance(second, dict) else None
            valid = isinstance(output, list) and second.get("status") == "completed" and any(
                isinstance(item, dict) and item.get("type") == "message"
                and isinstance(item.get("content"), list) and any(
                    isinstance(part, dict) and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str) and bool(part["text"].strip())
                    for part in item["content"]
                ) for item in output
            )
            result = ProbeAssessment(
                "responses_function_calls", "passed" if valid else "unknown",
                None if valid else "invalid_continuation",
            )
    except (asyncio.TimeoutError, ConnectionError, OSError):
        result = ProbeAssessment("responses_function_calls", "unavailable", "transport")
    store.record(provider, endpoint_id, model, result.feature, result.status)
    return result


def _is_expected_arguments(raw: str) -> bool:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(value, dict) and value.get("value") == "ok"
