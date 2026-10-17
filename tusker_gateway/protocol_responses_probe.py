"""Pure OpenAI Responses function-call contract classifier.

A completed Responses envelope can establish structural support for function
calls. A single turn cannot prove successful continuation with tool output.
"""
from __future__ import annotations

import json
from typing import Any

from tusker_gateway.protocol_probe import ProbeAssessment


def classify_responses_function_call(response: Any) -> ProbeAssessment:
    feature = "responses_function_calls"
    if not isinstance(response, dict) or not isinstance(response.get("output"), list):
        return ProbeAssessment(feature, "unknown", "invalid_responses_envelope")
    if response.get("status") not in (None, "completed"):
        return ProbeAssessment(feature, "unknown", "incomplete_response")
    for item in response["output"]:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        if item.get("name") != "protocol_probe_echo" or not isinstance(item.get("call_id"), str) or not item["call_id"]:
            continue
        arguments = item.get("arguments")
        if not isinstance(arguments, str):
            continue
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and parsed.get("value") == "ok":
            return ProbeAssessment(feature, "passed")
    return ProbeAssessment(feature, "unknown", "no_matching_function_call")
