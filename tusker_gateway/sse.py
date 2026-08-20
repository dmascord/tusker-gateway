"""SSE (Server-Sent Events) framing utilities."""
from __future__ import annotations

import json


def sse_frame(data: dict) -> bytes:
    """Frame a dict as an SSE data event.

    Yields a single 'data: <json>\n\n' line.
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


def sse_done() -> bytes:
    """Yield the [DONE] sentinel."""
    return b"data: [DONE]\n\n"


def sse_comment(comment: str) -> bytes:
    """Yield a comment line (prefixed with colon)."""
    return f": {comment}\n".encode("utf-8")


def format_openai_chunk(
    content: str | None = None,
    *,
    role: str | None = None,
    finish_reason: str | None = None,
    model: str | None = None,
) -> dict:
    """Build an OpenAI chat completion chunk dict."""
    chunk: dict = {"id": f"chatcmpl-{_rand_id()}", "choices": [], "model": model or "tusker-gateway"}

    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if role is not None:
        delta["role"] = role

    choice: dict = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason

    chunk["choices"].append(choice)
    return chunk


def _rand_id() -> str:
    """Generate a short random hex ID."""
    import secrets
    return secrets.token_hex(14)


def format_openai_response(
    content: str,
    *,
    finish_reason: str = "stop",
    model: str | None = None,
) -> dict:
    """Build a non-streaming OpenAI chat completion response dict."""
    return {
        "id": f"chatcmpl-{_rand_id()}",
        "object": "chat.completion",
        "created": _timestamp(),
        "model": model or "tusker-gateway",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _timestamp() -> int:
    """Current Unix timestamp."""
    import time
    return int(time.time())
