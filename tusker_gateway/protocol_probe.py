"""Pure behavioural classifiers for model protocol probes.

The caller must execute real, bounded requests; this module never sends
network traffic and never treats transport failures as lack of support.
No prompt, tool arguments, reasoning text, or response bodies are persisted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tusker_gateway.protocol_evidence import PROTOCOL_FEATURES


@dataclass(frozen=True)
class ProbeAssessment:
    feature: str
    status: str
    failure_class: str | None = None


def classify_probe(
    feature: str, *,
    response: dict[str, Any] | None = None,
    http_status: int | None = None,
    transport_error: bool = False,
) -> ProbeAssessment:
    """Evaluate an explicit probe against observed response structure.

    A generic successful completion is not sufficient evidence for reasoning
    replay, parameter support or correct tool execution.
    """
    if feature not in PROTOCOL_FEATURES:
        raise ValueError("unknown protocol feature")
    if transport_error or http_status in (408, 409, 425, 429) or (
        http_status is not None and http_status >= 500
    ):
        return ProbeAssessment(feature, "unavailable", "transient")
    if http_status in (400, 404, 405, 422):
        # Could be an invalid probe or a rejected model; without a known
        # structured error code, do not claim unsupported.
        return ProbeAssessment(feature, "unknown", "request_rejected")
    if not isinstance(response, dict):
        return ProbeAssessment(feature, "unknown", "missing_response")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ProbeAssessment(feature, "unknown", "invalid_envelope")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        return ProbeAssessment(feature, "unknown", "missing_message")
    calls = message.get("tool_calls")
    valid_calls = (
        isinstance(calls, list) and len(calls) > 0
        and all(
            isinstance(call, dict)
            and isinstance(call.get("id"), str) and bool(call["id"])
            and isinstance(call.get("function"), dict)
            and isinstance(call["function"].get("name"), str)
            and isinstance(call["function"].get("arguments"), str)
            and call["function"]["name"] == "protocol_probe_echo"
            and _echo_arguments_valid(call["function"]["arguments"])
            for call in calls
        )
    )
    if feature == "structured_tool_calls":
        return ProbeAssessment(feature, "passed" if valid_calls else "unknown")
    if feature == "tool_call_finish":
        return ProbeAssessment(
            feature, "passed" if valid_calls and choice.get("finish_reason") == "tool_calls" else "unknown"
        )
    if feature == "reasoning_channel":
        return ProbeAssessment(
            feature, "passed" if isinstance(message.get("reasoning_content"), str)
            and bool(message["reasoning_content"]) else "unknown"
        )
    # A single turn cannot prove multi-turn replay, parameter acceptance
    # or streamed fragment correctness. Their dedicated probes must
    # explicitly evaluate a full conversation/stream contract.
    return ProbeAssessment(feature, "unknown", "requires_specialized_probe")


def _json_valid(value: str) -> bool:
    try:
        json.loads(value)
        return True
    except (ValueError, TypeError):
        return False


def _echo_arguments_valid(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and parsed.get("value") == "ok"
