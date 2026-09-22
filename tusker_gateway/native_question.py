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

from tusker_gateway.question_adapters import (
    QuestionAdapter,
    current as current_question_adapter,
    render_question_arguments,
)

logger = logging.getLogger(__name__)

_QUESTION_TOOL_NAMES = frozenset({
    "ask",
    "question",
    "ask_question",
    "AskQuestion",
    "ask_followup_question",
})


def _is_question_tool_name(value: Any) -> bool:
    """Recognize native question names with client/provider namespaces."""
    if not isinstance(value, str):
        return False
    return value.rsplit(":", 1)[-1].strip() in _QUESTION_TOOL_NAMES

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
    """Bind approval to the latest user content, not replayed transcript history.

    OMP resends the full conversation and may append its own ask/tool turns on
    every retry. Hashing every historical user message made those harmless
    protocol additions look like a new risky request and generated another
    approval. The detector has already established that the latest user turn
    contains the risky phrase, so that turn is the correct approval boundary.
    """
    latest_user_content: Any = None
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            latest_user_content = message.get("content")
            break
    return hashlib.sha256(
        _canonical({"action": action, "user_content": latest_user_content}).encode()
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
        # Preserve paragraph and line boundaries so approval prompts remain
        # reviewable. Only normalize horizontal whitespace within each line;
        # collapsing ``\s`` here turns scripts and structured instructions
        # into an unreadable wall of text.
        preview = "\n".join(
            re.sub(r"[ \t]+", " ", line).rstrip()
            for line in content.splitlines()
        ).strip()
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


_SENSITIVE_ARGUMENT_KEY = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth(?:orization)?|credential|password|secret|token)"
)
_CREDENTIAL_VALUE = re.compile(
    r"(?i)\b(?:bearer\s+|sk-|gh[pousr]_)[A-Za-z0-9._~+/=-]{8,}"
)


def _redact_approval_value(value: Any, *, key: str = "") -> Any:
    """Redact credentials from values copied into an interactive prompt."""
    if _SENSITIVE_ARGUMENT_KEY.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(child_key): _redact_approval_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_approval_value(child) for child in value]
    if isinstance(value, str):
        return _CREDENTIAL_VALUE.sub("[redacted]", value)
    return value


def _call_approval_preview(calls: list[dict[str, Any]], *, max_chars: int = 600) -> str:
    """Describe the proposed tool call before asking the user to approve it.

    Tool output cannot be shown yet because the tool has not run. The prompt
    therefore shows the exact proposed arguments, with bounded length and
    credential-shaped values removed.
    """
    previews: list[str] = []
    for call in calls[:4]:
        function = call.get("function") or {}
        name = str(function.get("name") or "unknown")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except (TypeError, json.JSONDecodeError):
                arguments = _CREDENTIAL_VALUE.sub("[redacted]", arguments)
        arguments = _redact_approval_value(arguments)

        # Shell tools are much easier to review when the command is shown
        # directly instead of buried in a JSON object.
        if isinstance(arguments, dict) and isinstance(
            arguments.get("command") or arguments.get("cmd") or arguments.get("script"),
            str,
        ):
            command = arguments.get("command") or arguments.get("cmd") or arguments.get("script")
            previews.append(f"{name} command:\n{command}")
        else:
            previews.append(f"{name} arguments: {_canonical(arguments)}")

    if len(calls) > 4:
        previews.append(f"… and {len(calls) - 4} more tool call(s)")
    preview = "\n".join(previews) or "(no tool arguments were provided)"
    if len(preview) > max_chars:
        preview = preview[: max_chars - 1].rstrip() + "…"
    return preview


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
        for key in ("selectedOptions", "selected_options", "selectedOption", "selected_option"):
            if key in value:
                selected = value[key]
                if isinstance(selected, str):
                    selected = [selected]
                if isinstance(selected, list):
                    for option in selected:
                        found, approved = _extract_answer(option)
                        if found:
                            return found, approved
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


def _question_ids_from_call(function: Any) -> set[str]:
    """Extract approval IDs from an ask call's arguments.

    OMP/provider bridges can replace the tool-call ID with a namespaced value
    such as ``default_api:ask``.  The stable UUID is also present in the
    question payload, so prefer that identity when several approvals are
    pending rather than guessing based on insertion order.
    """
    if not isinstance(function, dict):
        return set()
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            return set()
    found: set[str] = set()
    if isinstance(arguments, dict):
        questions = arguments.get("questions")
        if isinstance(questions, list):
            for question in questions:
                if not isinstance(question, dict):
                    continue
                for key in ("id", "question_id", "questionId"):
                    candidate = question.get(key)
                    if isinstance(candidate, str) and candidate:
                        found.add(candidate)
        for key in ("id", "question_id", "questionId"):
            candidate = arguments.get(key)
            if isinstance(candidate, str) and candidate:
                found.add(candidate)
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
    adapter: QuestionAdapter | None = None,
) -> dict[str, Any] | None:
    """Convert a risky provider response into an OMP-native ask call."""
    _prune()
    adapter = adapter or current_question_adapter()
    action = _risky_action(calls)
    if action is None:
        return None
    signature = _calls_signature(calls)
    call_id = next(
        (
            pending_id
            for pending_id, pending in _PENDING.items()
            if pending.get("scope", "calls") == "calls"
            and pending.get("signature") == signature
            and pending.get("action") == action
        ),
        None,
    )
    if call_id is None:
        call_id = str(uuid.uuid4())
        _PENDING[call_id] = {
            "calls": json.loads(json.dumps(calls, ensure_ascii=False)),
            "signature": signature,
            "expires_at": time.time() + _TTL_SECS,
            "request_id": request_id or "unknown",
            "provider": provider or "unknown",
            "model": model or "unknown",
            "action": action,
            "adapter": adapter.key,
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
            "call_signature": signature,
            "decision": "pending",
        })
        logger.info(
            "native approval proposed request_id=%s approval_id=%s action=%s "
            "signature=%s provider=%s model=%s",
            request_id or "unknown",
            call_id,
            action,
            signature,
            provider or "unknown",
            model or "unknown",
        )
    else:
        pending = _PENDING[call_id]
        pending["expires_at"] = time.time() + _TTL_SECS
        logger.info(
            "native approval reused request_id=%s approval_id=%s action=%s "
            "signature=%s",
            request_id or "unknown",
            call_id,
            action,
            signature,
        )
    question_prompt = (
        f"Allow high-impact tool action '{action}'?\n"
        f"Proposed action:\n{_call_approval_preview(calls)}\n"
        "Review these arguments before approving."
    )
    question = render_question_arguments(
        adapter,
        question_id=call_id,
        header="Approval",
        prompt=question_prompt,
        options=[
            {"label": "Allow once", "description": "Execute this exact tool call once."},
            {"label": "Deny", "description": "Do not execute this tool call."},
        ],
    )
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
                    "function": {"name": adapter.tool_name, "arguments": _canonical(question)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }


def replay_approved_tool_response(
    messages: Any,
    *,
    request_id: str | None = None,
    audit: Any = None,
) -> dict[str, Any] | None:
    """Return the exact approved tool call without another model round trip.

    OMP may submit an ``ask`` selection as either a tool result or a user
    answer. Once the answer is bound to the pending question ID, replay the
    original normalized call directly to the client. The client remains the
    tool executor; the gateway only brokers approval and replay.
    """
    _prune()
    if not isinstance(messages, list):
        return None
    questions: set[str] = set()
    results: dict[str, Any] = {}
    unbound_results: list[Any] = []
    answer_found, answer_approved, _ = _latest_user_answer(messages)
    pending_call_ids = [
        call_id
        for call_id, pending in _PENDING.items()
        if pending.get("scope", "calls") == "calls"
    ]
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            calls = list(message.get("tool_calls") or [])
            # Some OpenAI-compatible clients downgrade a single tool call to
            # the legacy function_call field when replaying a transcript.
            if isinstance(message.get("function_call"), dict):
                calls.append(message["function_call"])
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                if not function and call.get("name"):
                    function = call
                if _is_question_tool_name(function.get("name")):
                    call_id = call.get("id")
                    if isinstance(call_id, str) and call_id in _PENDING:
                        questions.add(call_id)
                    else:
                        embedded_pending = _question_ids_from_call(function) & set(
                            pending_call_ids
                        )
                        if embedded_pending:
                            questions.update(embedded_pending)
                        elif len(pending_call_ids) == 1:
                            # OMP/provider bridges may replace the opaque ID
                            # with a namespaced value such as default_api:ask.
                            # Bind only when exactly one pending approval exists.
                            questions.add(pending_call_ids[0])
                    logger.info(
                        "approval replay question request_id=%s tool_id=%s "
                        "embedded_ids=%s bound_ids=%s pending=%d",
                        request_id or "unknown",
                        call_id or "none",
                        sorted(_question_ids_from_call(function)),
                        sorted(questions),
                        len(pending_call_ids),
                    )
        if message.get("role") in {"tool", "function"}:
            content = _message_content(message)
            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                result_id = str(tool_call_id)
                results[result_id] = content
                for embedded_id in _embedded_ids(content):
                    results[embedded_id] = content
                if result_id not in questions:
                    # A namespaced/default tool ID is still an answer, but
                    # cannot be used as the approval record key directly.
                    unbound_results.append(content)
            else:
                unbound_results.append(content)

    for call_id, pending in list(_PENDING.items()):
        if pending.get("scope", "calls") != "calls" or call_id not in questions:
            continue
        found, approved = _extract_answer(results.get(call_id))
        if not found and unbound_results:
            for content in unbound_results:
                found, approved = _extract_answer(content)
                if found:
                    break
        if not found and answer_found:
            found, approved = answer_found, answer_approved
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
            "call_signature": pending.get("signature"),
            "decision": "accepted" if approved else "denied",
            "execution_result": "not_observed",
            "replayed_directly": bool(approved),
        })
        if not approved:
            return None
        calls = pending.get("calls")
        if not isinstance(calls, list) or not calls:
            return None
        return {
            "id": "chatcmpl-" + secrets.token_hex(12),
            "object": "chat.completion",
            "model": pending.get("model") or "tusker-gateway",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": calls,
                },
                "finish_reason": "tool_calls",
            }],
        }
    return None


def question_response_for_content(
    messages: Any,
    action: str,
    *,
    model: str,
    matched_text: str | None = None,
    provider: str | None = None,
    request_id: str | None = None,
    audit: Any = None,
    adapter: QuestionAdapter | None = None,
) -> dict[str, Any]:
    """Convert a risky user-content request into an OMP-native ask call."""
    _prune()
    adapter = adapter or current_question_adapter()
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
            response = pending["response"]
            if pending.get("adapter", "omp") != adapter.key:
                # The approval is bound to the content/signature, not to the
                # presentation client. Re-render the existing pending prompt
                # for a harness that differs from the request which created
                # it, while retaining the same approval ID and audit record.
                old_call = (
                    ((response.get("choices") or [{}])[0].get("message") or {})
                    .get("tool_calls") or [{}]
                )[0]
                old_arguments = old_call.get("function", {}).get("arguments", "")
                try:
                    old_question = json.loads(old_arguments).get("questions", [{}])[0]
                    prompt = str(old_question.get("question") or "")
                except (TypeError, json.JSONDecodeError, AttributeError):
                    prompt = "Allow the model to continue this request?"
                rendered = render_question_arguments(
                    adapter,
                    question_id=str(old_call.get("id") or pending.get("approval_id")),
                    header="Approval",
                    prompt=prompt,
                    options=[
                        {
                            "label": "Allow once",
                            "description": "Continue this request with this approval only.",
                        },
                        {
                            "label": "Deny",
                            "description": "Stop this request without allowing the action.",
                        },
                    ],
                )
                response = json.loads(json.dumps(response, ensure_ascii=False))
                response["choices"][0]["message"]["tool_calls"][0]["function"] = {
                    "name": adapter.tool_name,
                    "arguments": _canonical(rendered),
                }
                pending["response"] = response
                pending["adapter"] = adapter.key
            logger.info(
                "reusing pending content question approval_id=%s request_id=%s",
                pending.get("approval_id", "unknown"),
                request_id or "unknown",
            )
            return response

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
        "adapter": adapter.key,
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
    question_prompt = (
        "The gateway detected this high-impact phrase in the user request: "
        f"\u201c{matched_text or _content_approval_preview(messages)}\u201d\n"
        "Allow the model to continue this request? Review the "
        "instruction and any proposed tool action before approving."
    )
    question = render_question_arguments(
        adapter,
        question_id=call_id,
        header="Approval",
        prompt=question_prompt,
        options=[
            {
                "label": "Allow once",
                "description": "Continue this request with this approval only.",
            },
            {
                "label": "Deny",
                "description": "Stop this request without allowing the action.",
            },
        ],
    )
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
                    "function": {"name": adapter.tool_name, "arguments": _canonical(question)},
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
    answer_found, answer_approved, _messages_without_answer = _latest_user_answer(messages)
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
                if _is_question_tool_name(function.get("name")) and call.get("id") in _PENDING:
                    questions[str(call["id"])] = call
        if message.get("role") in {"tool", "function"} and message.get("tool_call_id"):
            results[str(message["tool_call_id"])] = _message_content(message)
    for call_id, pending in list(_PENDING.items()):
        if pending.get("scope", "calls") != "calls":
            continue
        if pending.get("signature") != expected_signature or call_id not in questions:
            continue
        found, approved = _extract_answer(results.get(call_id))
        # OMP/OpenCode may submit the selected option as the next user turn
        # instead of a role=tool result. Keep the exact question/call binding
        # above; only the answer transport differs.
        if not found and answer_found:
            found, approved = answer_found, answer_approved
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

    def record_result(result_id: str, content: Any) -> None:
        """Keep every envelope; OMP may send an answer and a duplicate."""
        previous = results.get(result_id)
        if previous is None:
            results[result_id] = content
        elif isinstance(previous, list):
            previous.append(content)
        else:
            results[result_id] = [previous, content]

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
            record_result(tool_call_id, content)
            result_ids.add(tool_call_id)
            unbound_results.append(content)
            for embedded_id in _embedded_ids(content):
                record_result(embedded_id, content)
                result_ids.add(embedded_id)
        elif message.get("role") in {"tool", "function"}:
            # Some OMP-compatible clients put the ask question ID in the
            # result envelope rather than preserving tool_call_id.
            content = _message_content(message)
            unbound_results.append(content)
            for embedded_id in _embedded_ids(content):
                record_result(embedded_id, content)
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
