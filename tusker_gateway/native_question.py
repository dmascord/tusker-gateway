"""OpenCode/OMP native ``question`` tool approval bridge.

OMP already renders its built-in question tool as an interactive prompt.  We
use that ordinary tool-call path for approval and keep the original action
bound to the opaque question call id until the next request returns the
answer.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

_PENDING: dict[str, dict[str, Any]] = {}
_TTL_SECS = 300


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _calls_signature(calls: list[dict[str, Any]]) -> str:
    normalized = []
    for call in calls:
        function = call.get("function") or {}
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except (TypeError, json.JSONDecodeError):
                pass
        normalized.append({"name": str(function.get("name") or ""), "arguments": arguments})
    return hashlib.sha256(_canonical(normalized).encode()).hexdigest()


def _extract_answer(value: Any) -> tuple[bool, bool]:
    """Return (answer_found, approved) from OMP question-tool result shapes."""
    if isinstance(value, bool):
        return True, value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"allow", "allow once", "approve", "approved", "yes", "proceed"}:
            return True, True
        if normalized in {"deny", "denied", "no", "cancel", "reject", "拒否"}:
            return True, False
        return False, False
    if isinstance(value, dict):
        for key in ("approved", "allow", "allowed", "confirm", "confirmed"):
            if key in value and isinstance(value[key], bool):
                return True, value[key]
        for child in value.values():
            found, approved = _extract_answer(child)
            if found:
                return found, approved
    if isinstance(value, list):
        for child in value:
            found, approved = _extract_answer(child)
            if found:
                return found, approved
    return False, False


def _message_content(message: dict[str, Any]) -> Any:
    content = message.get("content")
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return content
    return content


def _prune() -> None:
    now = time.time()
    for call_id, pending in list(_PENDING.items()):
        if float(pending.get("expires_at", 0)) <= now:
            _PENDING.pop(call_id, None)


def _risky_action(calls: list[dict[str, Any]]) -> str | None:
    from tusker_gateway.endpoints import _high_impact_call_kind

    for call in calls:
        action = _high_impact_call_kind(call)
        if action:
            return action
    return None


def question_response_for_calls(calls: list[dict[str, Any]], *, model: str) -> dict[str, Any] | None:
    """Convert a risky provider response into an OMP-native question call."""
    _prune()
    action = _risky_action(calls)
    if action is None:
        return None
    call_id = "call_approval_" + secrets.token_urlsafe(12)
    _PENDING[call_id] = {
        "signature": _calls_signature(calls),
        "expires_at": time.time() + _TTL_SECS,
    }
    question = {
        "questions": [{
            "header": "Approval",
            "question": f"Allow high-impact tool action '{action}'?",
            "options": [
                {"label": "Allow once", "description": "Execute this exact tool call once."},
                {"label": "Deny", "description": "Do not execute this tool call."},
            ],
        }],
    }
    return {
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": "chat.completion",
        "model": model or "tusker-gateway",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "question", "arguments": _canonical(question)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }


def question_authorized(messages: Any, calls: list[dict[str, Any]]) -> bool:
    """Validate an OMP question result against the exact pending tool call."""
    _prune()
    if not isinstance(messages, list):
        return False
    expected_signature = _calls_signature(calls)
    questions: dict[str, dict[str, Any]] = {}
    results: dict[str, Any] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                if function.get("name") == "question" and call.get("id") in _PENDING:
                    questions[str(call["id"])] = call
        if message.get("role") in {"tool", "function"} and message.get("tool_call_id"):
            results[str(message["tool_call_id"])] = _message_content(message)
    for call_id, pending in list(_PENDING.items()):
        if pending.get("signature") != expected_signature or call_id not in questions:
            continue
        found, approved = _extract_answer(results.get(call_id))
        if not found:
            continue
        _PENDING.pop(call_id, None)
        return approved
    return False


def reset_pending() -> None:
    """Test helper; pending approvals are process-local and short-lived."""
    _PENDING.clear()
