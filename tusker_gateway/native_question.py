"""OpenCode/OMP native ``ask`` tool approval bridge.

OMP already renders its built-in ``ask`` tool as an interactive prompt.  We
use that ordinary tool-call path for approval and keep the original action
bound to the opaque question call id until the next request returns the
answer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_PENDING: dict[str, dict[str, Any]] = {}
_TTL_SECS = 300


def _audit(audit: Any, event: dict[str, Any]) -> None:
    """Best-effort audit emission; the safety decision remains fail-closed."""
    writer = getattr(audit, "write_sync", None)
    if writer is not None:
        writer(event)


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


def _content_signature(messages: Any, action: str) -> str:
    """Bind a content-level approval to the user text that triggered it."""
    user_content: list[Any] = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            user_content.append(message.get("content"))
    return hashlib.sha256(
        _canonical({"action": action, "user_content": user_content}).encode()
    ).hexdigest()


def _content_approval_preview(messages: Any, *, max_chars: int = 180) -> str:
    """Return a short explanation for a content approval prompt."""
    if not isinstance(messages, list):
        return "the user request"
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        preview = re.sub(r"\s+", " ", content).strip()
        # Keep credential-shaped values out of the prompt while retaining
        # enough context for the user to identify the request.
        preview = re.sub(
            r"(?i)\b(?:bearer\s+|sk-|gh[pousr]_)[A-Za-z0-9._~+/=-]{8,}",
            "[redacted]",
            preview,
        )
        if len(preview) > max_chars:
            preview = preview[: max_chars - 1].rstrip() + "…"
        return preview
    return "the user request"


def _extract_answer(value: Any) -> tuple[bool, bool]:
    """Return (answer_found, approved) from OMP ask-tool result shapes."""
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


def _latest_user_answer(messages: list[Any]) -> tuple[bool, bool, list[Any]]:
    """Recognize an OMP client that submits the selection as user text."""
    if not messages or not isinstance(messages[-1], dict):
        return False, False, messages
    if messages[-1].get("role") != "user":
        return False, False, messages
    content = messages[-1].get("content")
    found, approved = _extract_answer(content)
    if not found:
        return False, False, messages
    return True, approved, messages[:-1]


def _message_content(message: dict[str, Any]) -> Any:
    content = message.get("content")
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return content
    return content


def _embedded_ids(value: Any) -> set[str]:
    """Collect ask/question IDs from compatible tool-result envelopes."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key in ("id", "question_id", "tool_call_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                found.add(candidate)
        for child in value.values():
            found.update(_embedded_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_embedded_ids(child))
    return found


def _prune() -> None:
    now = time.time()
    for call_id, pending in list(_PENDING.items()):
        if float(pending.get("expires_at", 0)) <= now:
            _audit(pending.get("audit"), {
                "event_type": "tool.approval.decision",
                "approval_id": call_id,
                "request_id": pending.get("request_id", "unknown"),
                "provider": pending.get("provider", "unknown"),
                "model": pending.get("model", "unknown"),
                "action": pending.get("action", "unknown"),
                "call_signature": pending.get("signature"),
                "decision": "expired",
            })
            _PENDING.pop(call_id, None)


def _risky_action(calls: list[dict[str, Any]]) -> str | None:
    from tusker_gateway.endpoints import _high_impact_call_kind

    for call in calls:
        action = _high_impact_call_kind(call)
        if action:
            return action
    return None


def question_response_for_calls(
    calls: list[dict[str, Any]],
    *,
    model: str,
    provider: str | None = None,
    request_id: str | None = None,
    audit: Any = None,
) -> dict[str, Any] | None:
    """Convert a risky provider response into an OMP-native ask call."""
    _prune()
    action = _risky_action(calls)
    if action is None:
        return None
    approval_id = str(uuid.uuid4())
    call_id = approval_id
    _PENDING[call_id] = {
        "signature": _calls_signature(calls),
        "expires_at": time.time() + _TTL_SECS,
        "request_id": request_id or "unknown",
        "provider": provider or "unknown",
        "model": model or "unknown",
        "action": action,
        "audit": audit,
    }
    _audit(audit, {
        "event_type": "tool.approval.proposed",
        "approval_id": call_id,
        "request_id": request_id or "unknown",
        "provider": provider or "unknown",
        "model": model or "unknown",
        "action": action,
        "tool_names": [
            str((call.get("function") or {}).get("name") or "unknown")
            for call in calls[:8]
        ],
        "call_signature": _calls_signature(calls),
        "decision": "pending",
    })
    question = {
        "questions": [{
            "id": call_id,
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
                    # OMP/OpenCode calls this built-in interactive tool
                    # ``ask`` (not ``question``).
                    "function": {"name": "ask", "arguments": _canonical(question)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }


def question_response_for_content(
    messages: Any,
    action: str,
    *,
    model: str,
    matched_text: str | None = None,
    provider: str | None = None,
    request_id: str | None = None,
    audit: Any = None,
) -> dict[str, Any]:
    """Convert a risky user-content request into an OMP-native ask call."""
    _prune()
    signature = _content_signature(messages, action)
    # OMP can retry the original request while the interactive ask result is
    # being assembled. Reusing the pending response makes that retry
    # idempotent: it cannot create a second visible prompt or approval ID.
    for pending in _PENDING.values():
        if (
            pending.get("scope") == "content"
            and pending.get("action") == action
            and pending.get("signature") == signature
            and isinstance(pending.get("response"), dict)
        ):
            logger.info(
                "reusing pending content question approval_id=%s request_id=%s",
                pending.get("approval_id", "unknown"),
                request_id or "unknown",
            )
            return pending["response"]

    approval_id = str(uuid.uuid4())
    call_id = approval_id
    _PENDING[call_id] = {
        "approval_id": call_id,
        "signature": signature,
        "scope": "content",
        "expires_at": time.time() + _TTL_SECS,
        "request_id": request_id or "unknown",
        "provider": provider or "unknown",
        "model": model or "unknown",
        "action": action,
        "audit": audit,
    }
    _audit(audit, {
        "event_type": "tool.approval.proposed",
        "approval_id": call_id,
        "request_id": request_id or "unknown",
        "provider": provider or "unknown",
        "model": model or "unknown",
        "action": action,
        "tool_names": [],
        "call_signature": signature,
        "decision": "pending",
    })
    question = {
        "questions": [{
            "id": call_id,
            "header": "Approval",
            "question": (
                "The gateway detected this high-impact phrase in the user request: "
                f"\u201c{matched_text or _content_approval_preview(messages)}\u201d\n"
                "Allow the model to continue this request? Review the "
                "instruction and any proposed tool action before approving."
            ),
            "options": [
                {
                    "label": "Allow once",
                    "description": "Continue this request with this approval only.",
                },
                {
                    "label": "Deny",
                    "description": "Stop this request without allowing the action.",
                },
            ],
        }],
    }
    response = {
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
                    "function": {"name": "ask", "arguments": _canonical(question)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }
    _PENDING[call_id]["response"] = response
    return response


def question_authorized(
    messages: Any,
    calls: list[dict[str, Any]],
    *,
    request_id: str | None = None,
    audit: Any = None,
) -> bool:
    """Validate an OMP ask result against the exact pending tool call."""
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
                if function.get("name") in {"ask", "question"} and call.get("id") in _PENDING:
                    questions[str(call["id"])] = call
        if message.get("role") in {"tool", "function"} and message.get("tool_call_id"):
            results[str(message["tool_call_id"])] = _message_content(message)
    for call_id, pending in list(_PENDING.items()):
        if pending.get("scope", "calls") != "calls":
            continue
        if pending.get("signature") != expected_signature or call_id not in questions:
            continue
        found, approved = _extract_answer(results.get(call_id))
        if not found:
            continue
        _PENDING.pop(call_id, None)
        _audit(pending.get("audit") or audit, {
            "event_type": "tool.approval.decision",
            "approval_id": call_id,
            "request_id": request_id or "unknown",
            "original_request_id": pending.get("request_id", "unknown"),
            "provider": pending.get("provider", "unknown"),
            "model": pending.get("model", "unknown"),
            "action": pending.get("action", "unknown"),
            "call_signature": expected_signature,
            "decision": "accepted" if approved else "denied",
            "execution_result": "not_observed",
        })
        return approved
    return False


def question_authorized_for_content(
    messages: Any,
    action: str,
    *,
    request_id: str | None = None,
    audit: Any = None,
) -> bool:
    """Validate an OMP ask result for a content-level approval."""
    _prune()
    if not isinstance(messages, list):
        return False
    expected_signature = _content_signature(messages, action)
    answer_found, answer_approved, messages_without_answer = _latest_user_answer(messages)
    accepted_signatures = {expected_signature}
    if answer_found:
        accepted_signatures.add(_content_signature(messages_without_answer, action))
    questions: set[str] = set()
    result_ids: set[str] = set()
    results: dict[str, Any] = {}
    unbound_results: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                if function.get("name") in {"ask", "question"} and call.get("id") in _PENDING:
                    questions.add(str(call["id"]))
        if message.get("role") in {"tool", "function"} and message.get("tool_call_id"):
            tool_call_id = str(message["tool_call_id"])
            content = _message_content(message)
            results[tool_call_id] = content
            result_ids.add(tool_call_id)
            unbound_results.append(content)
            for embedded_id in _embedded_ids(content):
                results[embedded_id] = content
                result_ids.add(embedded_id)
        elif message.get("role") in {"tool", "function"}:
            # Some OMP-compatible clients put the ask question ID in the
            # result envelope rather than preserving tool_call_id.
            content = _message_content(message)
            unbound_results.append(content)
            for embedded_id in _embedded_ids(content):
                results[embedded_id] = content
                result_ids.add(embedded_id)
    unbound_answer_found, unbound_answer_approved = False, False
    for content in unbound_results:
        unbound_answer_found, unbound_answer_approved = _extract_answer(content)
        if unbound_answer_found:
            break
    logger.info(
        "content approval follow-up shape request_id=%s action=%s messages=%d roles=%s "
        "question_ids=%s result_ids=%s latest_user_answer=%s approved=%s "
        "unbound_answer=%s unbound_approved=%s pending_content=%d signature=%s "
        "accepted_signatures=%d",
        request_id or "unknown",
        action,
        len(messages),
        ",".join(
            str(message.get("role") or "?")
            for message in messages
            if isinstance(message, dict)
        ),
        ",".join(sorted(questions)) or "none",
        ",".join(sorted(result_ids)) or "none",
        answer_found,
        answer_approved if answer_found else "n/a",
        unbound_answer_found,
        unbound_answer_approved if unbound_answer_found else "n/a",
        sum(1 for pending in _PENDING.values() if pending.get("scope") == "content"),
        expected_signature[:12],
        len(accepted_signatures),
    )
    for call_id, pending in list(_PENDING.items()):
        if (
            pending.get("scope") != "content"
            or pending.get("action") != action
            or pending.get("signature") not in accepted_signatures
            or (
                call_id not in questions
                and call_id not in result_ids
                and not answer_found
                and not unbound_answer_found
            )
        ):
            continue
        found, approved = _extract_answer(results.get(call_id))
        if not found and answer_found:
            found, approved = answer_found, answer_approved
        if not found and unbound_answer_found:
            found, approved = unbound_answer_found, unbound_answer_approved
        if not found:
            continue
        _PENDING.pop(call_id, None)
        _audit(pending.get("audit") or audit, {
            "event_type": "tool.approval.decision",
            "approval_id": call_id,
            "request_id": request_id or "unknown",
            "original_request_id": pending.get("request_id", "unknown"),
            "provider": pending.get("provider", "unknown"),
            "model": pending.get("model", "unknown"),
            "action": action,
            "call_signature": expected_signature,
            "decision": "accepted" if approved else "denied",
            "execution_result": "not_observed",
        })
        return approved
    return False


def reset_pending() -> None:
    """Test helper; pending approvals are process-local and short-lived."""
    _PENDING.clear()
