"""Content-free tool-call health metrics for structured access logs.

Never record function names, arguments, IDs, reasoning text or prompt data.
"""
from __future__ import annotations

import json
from typing import Any


def summarize_chat_tool_result(result: dict[str, Any], *, requested: bool) -> dict[str, Any]:
    """Count malformed tool calls after provider normalization, without payloads."""
    calls: list[dict[str, Any]] = []
    reasoning_present = False
    finish_reason = ""
    choices = result.get("choices")
    if isinstance(choices, list):
        for choice in choices[:16]:
            if not isinstance(choice, dict):
                continue
            finish = choice.get("finish_reason")
            if isinstance(finish, str) and not finish_reason:
                finish_reason = finish[:48]
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            reasoning_present |= bool(message.get("reasoning_content"))
            raw = message.get("tool_calls")
            if isinstance(raw, list):
                calls.extend(c for c in raw[:128] if isinstance(c, dict))
    ids: set[str] = set()
    duplicates = missing = invalid = 0
    for call in calls:
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            missing += 1
        elif call_id in ids:
            duplicates += 1
        else:
            ids.add(call_id)
        function = call.get("function")
        if not isinstance(function, dict):
            invalid += 1
            continue
        args = function.get("arguments")
        if not isinstance(args, str):
            invalid += 1
            continue
        try:
            parsed = json.loads(args)
            if not isinstance(parsed, dict):
                invalid += 1
        except (ValueError, TypeError):
            invalid += 1
    return {
        "requested": requested,
        "returned": len(calls),
        "invalid_arguments": invalid,
        "missing_ids": missing,
        "duplicate_ids": duplicates,
        "reasoning_present": reasoning_present,
        "protocol": "openai_chat",
        "finish_reason": finish_reason,
    }
