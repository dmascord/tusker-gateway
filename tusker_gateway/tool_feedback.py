"""Bounded, privacy-safe correlation of OpenAI tool calls and returned results.

Only keyed digests of call IDs are retained; never store arguments or outputs.
Correlation is best-effort within one process, not proof of tool execution.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any
from tusker_gateway import tool_feedback_store

def _digest(call_id: str) -> str:
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()


def register_generated_calls(call_ids: list[str], *, request_id: str | None = None) -> int:
    """Register emitted IDs after normalization; never persist raw IDs."""
    return tool_feedback_store.register(call_ids, request_id)

def _feedback_kind(content: Any) -> str:
    """Recognise explicit structured failures, never infer success from prose."""
    if isinstance(content, dict):
        if content.get("isError") is True or content.get("is_error") is True:
            return "error"
        if content.get("resultType") == "error" or content.get("result_type") == "error":
            return "error"
        if content.get("status") in ("error", "failed", "rejected", "timeout"):
            return "error"
    if isinstance(content, str) and content.strip() == "Tool result missing":
        return "synthetic_missing"
    return "unknown"


def correlate_chat_messages(messages: Any) -> dict[str, int]:
    """Correlate tool feedback, ignoring historical results in replayed context."""
    counts = {
        "result_messages": 0, "matched_results": 0, "untracked_results": 0,
        "duplicate_results": 0, "historical_results": 0,
        "explicit_errors": 0, "synthetic_missing": 0,
    }
    if not isinstance(messages, list):
        return counts
    recent_assistant = max(
        (i for i, msg in enumerate(messages[:512])
         if isinstance(msg, dict) and msg.get("role") == "assistant"),
        default=-1,
    )
    seen: set[str] = set()
    for index, msg in enumerate(messages[:512]):
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        call_id = msg.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if index < recent_assistant:
            counts["historical_results"] += 1
            continue
        counts["result_messages"] += 1
        digest = _digest(call_id)
        if digest in seen:
            counts["duplicate_results"] += 1
            continue
        seen.add(digest)
        if not tool_feedback_store.consume(call_id):
            counts["untracked_results"] += 1
            continue
        counts["matched_results"] += 1
        kind = _feedback_kind(msg.get("content"))
        if kind == "error":
            counts["explicit_errors"] += 1
        elif kind == "synthetic_missing":
            counts["synthetic_missing"] += 1
    return counts


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
