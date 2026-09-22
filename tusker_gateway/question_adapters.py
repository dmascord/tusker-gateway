"""Client-specific interactive-question adapters.

There is no universal OpenAI wire shape for an approval prompt.  This module
keeps the gateway's approval record provider-neutral and only adapts the
client-facing question tool at the boundary.

The adapter is selected from an explicit ``X-Tusker-Harness`` header first,
then conservative user-agent/metadata hints.  Unknown clients deliberately
use the OMP-compatible shape; guessing a host permission protocol would be a
security regression because those clients approve tools outside the model
conversation.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QuestionAdapter:
    key: str
    tool_name: str
    shape: str
    host_permission: bool = False


_ADAPTERS: dict[str, QuestionAdapter] = {
    # OMP and OpenCode v1 use the same questions/options envelope. OpenCode
    # v2 calls the built-in tool ``question``.
    "omp": QuestionAdapter("omp", "ask", "omp"),
    "opencode": QuestionAdapter("opencode", "question", "omp"),
    "cline": QuestionAdapter("cline", "ask_question", "cline"),
    "roo": QuestionAdapter("roo", "ask_followup_question", "cline"),
    "continue": QuestionAdapter("continue", "AskQuestion", "continue"),
    # These clients gate tools in their own host UI/hooks. We retain the
    # OMP fallback until a signed/explicit host-permission handshake exists;
    # an HTTP user-agent alone must never disable the gateway's guard.
    "cursor": QuestionAdapter("cursor", "ask", "omp", host_permission=True),
    "claude_code": QuestionAdapter("claude_code", "ask", "omp", host_permission=True),
    "codex_cli": QuestionAdapter("codex_cli", "ask", "omp", host_permission=True),
    "gemini_cli": QuestionAdapter("gemini_cli", "ask", "omp", host_permission=True),
    "aider": QuestionAdapter("aider", "ask", "omp", host_permission=True),
}

_DEFAULT = _ADAPTERS["omp"]
_CURRENT: ContextVar[QuestionAdapter] = ContextVar(
    "tusker_question_adapter", default=_DEFAULT
)


def adapter_for_name(name: str | None) -> QuestionAdapter:
    if not isinstance(name, str):
        return _DEFAULT
    normalized = name.strip().lower().replace("-", "_")
    aliases = {
        "open_code": "opencode",
        "open-code": "opencode",
        "roo_code": "roo",
        "roocode": "roo",
        "continue_dev": "continue",
        "claude": "claude_code",
        "codex": "codex_cli",
        "gemini": "gemini_cli",
    }
    return _ADAPTERS.get(aliases.get(normalized, normalized), _DEFAULT)


def detect_adapter(headers: Any = None, body: dict[str, Any] | None = None) -> QuestionAdapter:
    """Detect a client without trusting weak hints to weaken safety."""
    headers = headers or {}
    explicit = (
        headers.get("X-Tusker-Harness")
        or headers.get("X-Client-Harness")
        or (body or {}).get("harness")
        or ((body or {}).get("metadata") or {}).get("harness")
    )
    if explicit:
        return adapter_for_name(str(explicit))
    user_agent = str(headers.get("User-Agent") or "").lower()
    for marker, name in (
        ("opencode", "opencode"),
        ("open-code", "opencode"),
        ("cline", "cline"),
        ("roo", "roo"),
        ("continue", "continue"),
        ("cursor", "cursor"),
        ("claude-code", "claude_code"),
        ("codex", "codex_cli"),
        ("gemini", "gemini_cli"),
        ("aider", "aider"),
    ):
        if marker in user_agent:
            return _ADAPTERS[name]
    return _DEFAULT


def set_current(adapter: QuestionAdapter) -> None:
    _CURRENT.set(adapter)


def current() -> QuestionAdapter:
    return _CURRENT.get()


def render_question_arguments(
    adapter: QuestionAdapter,
    *,
    question_id: str,
    prompt: str,
    header: str,
    options: list[dict[str, str]],
) -> dict[str, Any]:
    """Build the native argument envelope for a supported question tool."""
    if adapter.shape == "cline":
        return {
            "question": prompt,
            "follow_up": [
                {"text": option["label"], "mode": "code"}
                for option in options
            ],
            "question_id": question_id,
        }
    if adapter.shape == "continue":
        return {
            "id": question_id,
            "question": prompt,
            "header": header,
            "options": options,
        }
    return {
        "questions": [{
            "id": question_id,
            "header": header,
            "question": prompt,
            "options": options,
        }],
    }


def supported_names() -> tuple[str, ...]:
    return tuple(_ADAPTERS)
