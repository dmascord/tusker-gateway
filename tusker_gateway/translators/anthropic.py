"""Anthropic Messages API ↔ OpenAI Chat Completions translator.

This module is pure: no HTTP, no pool routing, no auth. It translates body
shapes in both directions and registers itself with
:mod:`tusker_gateway.translators` on import.

What's translated:

- **Request** (``Anthropic → OpenAI``): ``system`` → system message, message
  content blocks → text/image blocks, tools (``input_schema`` → ``parameters``),
  ``max_tokens`` / ``stop_sequences`` → ``max_tokens`` / ``stop``.
- **Response** (``OpenAI → Anthropic``): message content → ``text`` content
  block, ``tool_calls`` → ``tool_use`` content blocks, ``finish_reason`` →
  ``stop_reason``, ``usage`` → ``input_tokens`` / ``output_tokens``.
- **Streaming** (``OpenAI SSE → Anthropic SSE``): emits the
  ``message_start`` → ``content_block_start`` → ``content_block_delta`` →
  ``content_block_stop`` → ``message_delta`` → ``message_stop`` lifecycle.

The streaming translator takes parsed OpenAI chunks (already decoded from
SSE by the HTTP layer) and returns raw Anthropic SSE bytes. The HTTP layer
is responsible for flushing bytes to the client.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from tusker_gateway.errors import BadRequestError
from tusker_gateway.translators import (
    ANTHROPIC,
    register_request,
    register_response,
    register_streaming,
)


# ---------------------------------------------------------------------------
# Request: Anthropic → OpenAI
# ---------------------------------------------------------------------------


def _convert_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool definitions to OpenAI function-calling format."""
    openai_tools: list[dict[str, Any]] = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return openai_tools


def request_anthropic_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic Messages API request body to OpenAI chat format.

    Handles:

    - ``system`` (string or list of content blocks) → system message
    - ``messages`` with str or list content → joined text or vision blocks
    - Parameter mapping: ``max_tokens``, ``temperature``, ``top_p``,
      ``stop_sequences`` → ``stop``, ``stream``, ``model``
    - ``tools`` (``input_schema`` → OpenAI ``parameters``)

    Raises :class:`BadRequestError` for malformed image sources.
    """
    openai: dict[str, Any] = {}

    # Model & stream pass through directly.
    if "model" in body:
        openai["model"] = body["model"]
    if "stream" in body:
        openai["stream"] = bool(body["stream"])

    # System prompt → system message prepended to messages list.
    system = body.get("system")
    messages: list[dict[str, Any]] = []
    if system is not None:
        if isinstance(system, str):
            text = system
        elif isinstance(system, list):
            parts: list[str] = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            text = "\n".join(parts)
        else:
            text = str(system)
        messages.append({"role": "system", "content": text})

    # Convert Anthropic messages → OpenAI messages.
    for msg in body.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            has_image = any(
                isinstance(block, dict) and block.get("type") == "image"
                for block in content
            )
            if not has_image:
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, dict) and block.get("type") in {"tool_use", "tool_result"}:
                        parts.append(json.dumps(block))
                    elif isinstance(block, str):
                        parts.append(block)
                messages.append({"role": role, "content": "\n".join(parts)})
                continue

            blocks: list[dict[str, Any]] = []
            for block in content:
                if isinstance(block, str):
                    blocks.append({"type": "text", "text": block})
                    continue
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    blocks.append({"type": "text", "text": block.get("text", "")})
                elif block_type == "image":
                    source = block.get("source", {})
                    source_type = source.get("type")
                    if source_type == "base64":
                        media_type = source.get("media_type")
                        data = source.get("data")
                        if not isinstance(media_type, str) or not isinstance(data, str) or not data:
                            raise BadRequestError(
                                "Anthropic base64 image source requires media_type and data",
                                code="invalid_image_source",
                            )
                        blocks.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{data}"},
                        })
                    elif source_type == "url":
                        url = source.get("url")
                        if not isinstance(url, str) or not url:
                            raise BadRequestError(
                                "Anthropic URL image source requires a non-empty URL",
                                code="invalid_image_source",
                            )
                        blocks.append({"type": "image_url", "image_url": {"url": url}})
                    else:
                        raise BadRequestError(
                            f"Anthropic image source type '{source_type}' cannot be converted to an image URL",
                            code="unsupported_image_source",
                        )
                elif block_type in {"tool_use", "tool_result"}:
                    blocks.append({"type": "text", "text": json.dumps(block)})
            messages.append({"role": role, "content": blocks})
        elif content is None:
            messages.append({"role": role, "content": ""})
        else:
            messages.append({"role": role, "content": str(content)})

    openai["messages"] = messages

    # Parameter mapping.
    if "max_tokens" in body:
        openai["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        openai["temperature"] = body["temperature"]
    if "top_p" in body:
        openai["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        openai["stop"] = body["stop_sequences"]

    # Tool definitions (Anthropic → OpenAI format).
    if "tools" in body:
        openai["tools"] = _convert_anthropic_tools(body["tools"])
    if "tool_choice" in body:
        choice = body["tool_choice"]
        if isinstance(choice, dict):
            choice_type = choice.get("type")
            if choice_type == "auto":
                openai["tool_choice"] = "auto"
            elif choice_type == "any":
                openai["tool_choice"] = "required"
            elif choice_type == "tool" and isinstance(choice.get("name"), str):
                openai["tool_choice"] = {
                    "type": "function",
                    "function": {"name": choice["name"]},
                }

    return openai


# ---------------------------------------------------------------------------
# Response: OpenAI → Anthropic (non-streaming)
# ---------------------------------------------------------------------------

# Stop-reason mapping (OpenAI → Anthropic).
_STOP_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}


def response_openai_to_anthropic(
    result: dict[str, Any],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Convert a non-streaming OpenAI chat completion response to Anthropic.

    ``state`` may carry ``original_model`` (the Anthropic model name the
    client requested) so the response echoes the same identifier. Callers
    that don't care can pass ``{}``.
    """
    message = result.get("choices", [{}])[0].get("message", {})
    content_text = message.get("content") or ""

    content: list[dict[str, Any]] = []
    if content_text:
        content.append({"type": "text", "text": content_text})

    # Convert tool_calls if present.
    for tc in message.get("tool_calls", []):
        func = tc.get("function", {})
        arguments = func.get("arguments", "{}")
        if arguments and not isinstance(arguments, str):
            arguments_str = json.dumps(arguments, ensure_ascii=False)
        else:
            arguments_str = arguments or "{}"
        try:
            input_payload = json.loads(arguments_str)
        except json.JSONDecodeError:
            input_payload = {}
        content.append({
            "type": "tool_use",
            "id": f"toolu_{secrets.token_hex(8)}",
            "name": func.get("name", ""),
            "input": input_payload,
        })

    openai_finish = result.get("choices", [{}])[0].get("finish_reason", "stop")
    stop_reason = _STOP_REASON_MAP.get(openai_finish, "end_turn")

    openai_usage = result.get("usage", {})
    original_model = state.get("original_model", "") if state else ""

    return [{
        "id": f"msg_{secrets.token_hex(12)}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": original_model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(openai_usage.get("prompt_tokens", 0)),
            "output_tokens": int(openai_usage.get("completion_tokens", 0)),
        },
    }]


# ---------------------------------------------------------------------------
# Streaming: OpenAI SSE → Anthropic SSE
# ---------------------------------------------------------------------------

# State keys used by the streaming translator.
_STREAM_KEY_MSG_ID = "msg_id"
_STREAM_KEY_STARTED = "started"
_STREAM_KEY_CLOSED = "closed"
_STREAM_KEY_INPUT_TOKENS = "input_tokens"
_STREAM_KEY_OUTPUT_TOKENS = "output_tokens"
_STREAM_KEY_LAST_STOP_REASON = "last_stop_reason"


def init_anthropic_stream_state(
    *,
    model: str = "",
    input_tokens: int = 0,
    msg_id: str | None = None,
) -> dict[str, Any]:
    """Create a fresh per-stream state dict for the Anthropic streaming translator.

    The HTTP layer is expected to populate ``model`` and ``input_tokens`` via
    ``update_stream_state`` after creating the state.
    """
    return {
        "model": model,
        _STREAM_KEY_MSG_ID: msg_id or f"msg_{secrets.token_hex(12)}",
        _STREAM_KEY_STARTED: False,
        _STREAM_KEY_CLOSED: False,
        _STREAM_KEY_INPUT_TOKENS: input_tokens,
        _STREAM_KEY_OUTPUT_TOKENS: 0,
        _STREAM_KEY_LAST_STOP_REASON: "end_turn",
    }


def update_stream_state(
    state: dict[str, Any],
    *,
    model: str | None = None,
    input_tokens: int | None = None,
    msg_id: str | None = None,
) -> None:
    """Update mutable stream state fields before streaming begins."""
    if model is not None:
        state["model"] = model
    if input_tokens is not None:
        state[_STREAM_KEY_INPUT_TOKENS] = input_tokens
    if msg_id is not None:
        state[_STREAM_KEY_MSG_ID] = msg_id


def _sse_frame(event: str, payload: dict[str, Any]) -> bytes:
    """Encode a single Anthropic SSE event to bytes."""
    return "\n".join([
        f"event: {event}",
        f"data: {json.dumps(payload, ensure_ascii=False)}",
        "",
    ]).encode("utf-8")


def _message_start_frame(state: dict[str, Any]) -> bytes:
    """Build the message_start + content_block_start sequence."""
    payload = {
        "type": "message_start",
        "message": {
            "id": state[_STREAM_KEY_MSG_ID],
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": state["model"],
            "stop_reason": None,
            "usage": {
                "input_tokens": state[_STREAM_KEY_INPUT_TOKENS],
                "output_tokens": 0,
            },
        },
    }
    start_block = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    return (
        _sse_frame("message_start", payload)
        + _sse_frame("content_block_start", start_block)
    )


def _text_delta_frame(text: str) -> bytes:
    payload = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": text},
    }
    return _sse_frame("content_block_delta", payload)


def _closing_sequence(state: dict[str, Any]) -> bytes:
    """Build content_block_stop + message_delta + message_stop."""
    stop_block = {"type": "content_block_stop", "index": 0}
    delta_payload = {
        "type": "message_delta",
        "delta": {
            "stop_reason": state[_STREAM_KEY_LAST_STOP_REASON],
            "stop_sequence": None,
        },
        "usage": {"output_tokens": state[_STREAM_KEY_OUTPUT_TOKENS]},
    }
    stop_payload = {"type": "message_stop"}
    return (
        _sse_frame("content_block_stop", stop_block)
        + _sse_frame("message_delta", delta_payload)
        + _sse_frame("message_stop", stop_payload)
    )


def translate_openai_chunk_to_anthropic(
    chunk: dict[str, Any],
    state: dict[str, Any],
) -> list[bytes]:
    """Translate one OpenAI streaming chunk to Anthropic SSE bytes.

    On the first call, emits the ``message_start`` + ``content_block_start``
    sequence and then continues to content processing for the same chunk
    (the first OpenAI chunk frequently carries the initial text). Subsequent
    calls emit ``content_block_delta`` for content chunks. After the upstream
    signals ``[DONE]`` (the HTTP layer passes ``None`` as the chunk
    argument to the final invocation) the closing sequence is emitted.

    State mutation: tracks output_tokens (estimated from chunk text length
    when usage isn't carried by upstream), last_stop_reason, and the
    started/closed flags.
    """
    # Closing the stream — caller passes None to signal end-of-stream.
    if chunk is None:
        if state[_STREAM_KEY_CLOSED]:
            return []
        state[_STREAM_KEY_CLOSED] = True
        return [_closing_sequence(state)]

    # Already closed — drop everything else.
    if state[_STREAM_KEY_CLOSED]:
        return []

    out: list[bytes] = []

    # First call → emit message_start + content_block_start before any
    # content from this same chunk. Some upstreams bundle the role and
    # first text delta in the same frame; we need both.
    if not state[_STREAM_KEY_STARTED]:
        state[_STREAM_KEY_STARTED] = True
        out.append(_message_start_frame(state))

    choices = chunk.get("choices") or []
    if choices:
        choice = choices[0]
        delta = choice.get("delta") or {}
        content_text = delta.get("content")
        finish_reason = choice.get("finish_reason")

        if content_text:
            state[_STREAM_KEY_OUTPUT_TOKENS] += max(1, len(content_text) // 4)
            out.append(_text_delta_frame(content_text))

        if finish_reason:
            state[_STREAM_KEY_LAST_STOP_REASON] = _STOP_REASON_MAP.get(
                finish_reason, "end_turn"
            )

    # Honor upstream-provided usage if present.
    usage = chunk.get("usage") or {}
    if usage.get("completion_tokens"):
        state[_STREAM_KEY_OUTPUT_TOKENS] = int(usage["completion_tokens"])

    return out


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

register_request(ANTHROPIC, request_anthropic_to_openai)
register_response(ANTHROPIC, response_openai_to_anthropic)
register_streaming(
    ANTHROPIC,
    # state_factory takes no args — the HTTP layer mutates fields via
    # update_stream_state() once it knows the model and token estimate.
    state_factory=lambda: init_anthropic_stream_state(),
    chunk_translator=translate_openai_chunk_to_anthropic,
)


__all__ = [
    "request_anthropic_to_openai",
    "response_openai_to_anthropic",
    "translate_openai_chunk_to_anthropic",
    "init_anthropic_stream_state",
    "update_stream_state",
]
