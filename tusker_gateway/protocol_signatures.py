"""Provider reasoning/signature preservation helpers.

Never reconstruct provider signatures from visible reasoning. Keep opaque
provider-owned fields intact during replay and avoid passing them to
unrelated upstream providers.
"""
from __future__ import annotations

from typing import Any

# Fields known to carry opaque provider continuation state.
_OPAQUE_KEYS = frozenset({
    "thought_signature", "thinking_signature", "signature",
    "encrypted_content", "redacted_thinking",
})
_REASONING_KEYS = frozenset({
    "reasoning_content", "reasoning", "reasoning_details",
    "thinking", "thinking_blocks",
})


def copy_replay_message(message: dict[str, Any]) -> dict[str, Any]:
    """Preserve provider-owned reasoning payloads without mutating history."""
    copied = dict(message)
    if isinstance(message.get("tool_calls"), list):
        copied["tool_calls"] = [
            dict(call) if isinstance(call, dict) else call
            for call in message["tool_calls"]
        ]
    if isinstance(message.get("content"), list):
        copied["content"] = [
            dict(part) if isinstance(part, dict) else part
            for part in message["content"]
        ]
    return copied


def strip_cross_provider_replay(
    message: dict[str, Any], *, same_upstream: bool,
) -> dict[str, Any]:
    """Remove opaque replay state only when switching upstream providers.

    Keep ordinary content and structured tool calls; reasoning text is not
    promoted into content. This does not alter the original conversation.
    """
    copied = copy_replay_message(message)
    if same_upstream:
        return copied
    for key in _OPAQUE_KEYS | _REASONING_KEYS:
        copied.pop(key, None)
    if isinstance(copied.get("content"), list):
        cleaned = []
        for part in copied["content"]:
            if isinstance(part, dict):
                if part.get("type") in {"thinking", "redacted_thinking", "reasoning"}:
                    continue
                part = {key: value for key, value in part.items() if key not in _OPAQUE_KEYS}
            cleaned.append(part)
        copied["content"] = cleaned
    if isinstance(copied.get("tool_calls"), list):
        copied["tool_calls"] = [
            {key: value for key, value in call.items() if key not in _OPAQUE_KEYS}
            if isinstance(call, dict) else call
            for call in copied["tool_calls"]
        ]
    return copied
