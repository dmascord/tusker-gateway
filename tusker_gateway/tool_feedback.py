"""Bounded, privacy-safe correlation of OpenAI tool calls and returned results.

Only keyed digests of call IDs are retained; never store arguments or outputs.
Correlation is best-effort within one process, not proof of tool execution.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import OrderedDict
from typing import Any
from tusker_gateway import tool_feedback_store

_LOCK = threading.Lock()
_SECRET = os.urandom(32)
_PENDING: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_TTL = 3600.0
_MAX_PENDING = 8192


def _digest(call_id: str) -> str:
    return hmac.new(_SECRET, call_id.encode(), hashlib.sha256).hexdigest()


def _expire(now: float) -> None:
    while _PENDING and (len(_PENDING) > _MAX_PENDING or next(iter(_PENDING.values()))[0] < now - _TTL):
        _PENDING.popitem(last=False)


def register_generated_calls(call_ids: list[str], *, request_id: str | None = None) -> int:
    """Register emitted IDs after normalization; never persist raw IDs."""
    return tool_feedback_store.register(call_ids, request_id)

def correlate_chat_messages(messages: Any) -> dict[str, int]:
    """Observe standard role=tool feedback in an incoming chat request.

    Repeated conversation history must not be interpreted as repeated
    executions. A previously consumed ID is reported as untracked.
    """
    if not isinstance(messages, list):
        return {"result_messages": 0, "matched_results": 0, "untracked_results": 0,
                "duplicate_results": 0}
    ids = []
    for message in messages[:512]:
        if isinstance(message, dict) and message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                ids.append(call_id)
    matched = untracked = duplicate = 0
    seen: set[str] = set()
    for call_id in ids:
        digest = _digest(call_id)
        if digest in seen:
            duplicate += 1
            continue
        seen.add(digest)
        if tool_feedback_store.consume(call_id):
            matched += 1
        else:
            untracked += 1
    return {"result_messages": len(ids), "matched_results": matched,
            "untracked_results": untracked, "duplicate_results": duplicate}


def chat_result_call_ids(result: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for choice in result.get("choices", [])[:16]:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        for call in (message.get("tool_calls") or [])[:128]:
            if isinstance(call, dict) and isinstance(call.get("id"), str):
                ids.append(call["id"])
    return ids


def response_result_call_ids(items: Any) -> list[str]:
    """Return generated Responses function call IDs."""
    if not isinstance(items, list):
        return []
    return [item["call_id"] for item in items[:128]
            if isinstance(item, dict) and item.get("type") == "function_call"
            and isinstance(item.get("call_id"), str) and item["call_id"]]


def correlate_responses_input(items: Any) -> dict[str, int]:
    """Reuse Chat Completions correlation for standard Responses tool feedback."""
    if not isinstance(items, list):
        return correlate_chat_messages(None)
    messages = [{"role": "tool", "tool_call_id": item.get("call_id"),
                 "content": item.get("output")}
                for item in items[:512] if isinstance(item, dict)
                and item.get("type") == "function_call_output"]
    return correlate_chat_messages(messages)
