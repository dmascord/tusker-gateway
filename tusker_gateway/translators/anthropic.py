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


def _json_arguments(value: Any) -> str:
    """Return function arguments in the string form used by OpenAI tools."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"raw": str(value)}, ensure_ascii=False)


def _anthropic_block_to_openai(block: Any) -> dict[str, Any] | None:
    """Convert one ordinary Anthropic content block without dropping it."""
    if isinstance(block, str):
        return {"type": "text", "text": block}
    if not isinstance(block, dict):
        return {"type": "text", "text": str(block)}

    block_type = block.get("type")
    if block_type == "text":
        return {"type": "text", "text": str(block.get("text") or "")}
    if block_type == "image":
        source = block.get("source") or {}
        source_type = source.get("type")
        if source_type == "base64":
            media_type = source.get("media_type")
            data = source.get("data")
            if not isinstance(media_type, str) or not isinstance(data, str) or not data:
                raise BadRequestError(
                    "Anthropic base64 image source requires media_type and data",
                    code="invalid_image_source",
                )
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"},
            }
        if source_type == "url":
            url = source.get("url")
            if not isinstance(url, str) or not url:
                raise BadRequestError(
                    "Anthropic URL image source requires a non-empty URL",
                    code="invalid_image_source",
                )
            return {"type": "image_url", "image_url": {"url": url}}
        raise BadRequestError(
            f"Anthropic image source type '{source_type}' cannot be converted to an image URL",
            code="unsupported_image_source",
        )

    # Anthropic has more content block types than the canonical OpenAI chat
    # shape. Keep unsupported blocks inspectable as text instead of silently
    # deleting them from the conversation.
    if block_type in {"tool_use", "tool_result"}:
        return None
    return {"type": "text", "text": json.dumps(block, ensure_ascii=False)}


def _openai_content_value(blocks: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Use compact text for text-only content and blocks for multimodal input."""
    if any(block.get("type") == "image_url" for block in blocks):
        return blocks
    return "\n".join(
        str(block.get("text") or "")
        for block in blocks
        if block.get("type") == "text"
    )


def _anthropic_tool_result_content(content: Any) -> Any:
    """Convert an Anthropic tool result while retaining structured payloads."""
    if isinstance(content, str) or content is None:
        return content if content is not None else ""
    if isinstance(content, list):
        blocks = [
            converted
            for block in content
            if (converted := _anthropic_block_to_openai(block)) is not None
        ]
        return _openai_content_value(blocks)
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _anthropic_tool_use_to_openai(block: dict[str, Any], index: int) -> dict[str, Any]:
    """Convert an Anthropic ``tool_use`` block to a Chat Completions call."""
    name = str(block.get("name") or "").strip()
    call_id = str(block.get("id") or f"call_anthropic_{index}").strip()
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": _json_arguments(block.get("input")),
        },
    }


def _anthropic_tool_result_to_openai(block: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic ``tool_result`` block to a tool message."""
    message: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": str(block.get("tool_use_id") or block.get("id") or ""),
        "content": _anthropic_tool_result_content(block.get("content")),
    }
    # Chat Completions has no standard error flag, but retaining this field is
    # safer than turning a failed tool execution into an apparent success.
    if block.get("is_error") is True:
        message["is_error"] = True
    return message


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
            converted_blocks = []
            for block in system:
                converted = _anthropic_block_to_openai(block)
                if converted is None:
                    # Tool blocks are not valid Anthropic system content, but
                    # if a compatibility client sends one, do not erase it
                    # before the upstream can report the malformed request.
                    converted = {
                        "type": "text",
                        "text": json.dumps(block, ensure_ascii=False),
                    }
                converted_blocks.append(converted)
            text = _openai_content_value(converted_blocks)
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
            tool_uses = [
                block for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            tool_results = [
                block for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]

            if role == "assistant" and tool_uses:
                ordinary_blocks = [
                    converted
                    for block in content
                    if not (isinstance(block, dict) and block.get("type") == "tool_use")
                    if not (isinstance(block, dict) and block.get("type") == "tool_result")
                    if (converted := _anthropic_block_to_openai(block)) is not None
                ]
                messages.append({
                    "role": "assistant",
                    "content": _openai_content_value(ordinary_blocks),
                    "tool_calls": [
                        _anthropic_tool_use_to_openai(block, index)
                        for index, block in enumerate(tool_uses)
                    ],
                })
                # Be tolerant of a non-standard assistant message carrying a
                # tool result; retain it rather than flattening it into prose.
                messages.extend(_anthropic_tool_result_to_openai(block) for block in tool_results)
                continue

            if role == "user" and tool_results:
                ordinary_blocks: list[dict[str, Any]] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        if ordinary_blocks:
                            messages.append({
                                "role": "user",
                                "content": _openai_content_value(ordinary_blocks),
                            })
                            ordinary_blocks = []
                        messages.append(_anthropic_tool_result_to_openai(block))
                        continue
                    converted = _anthropic_block_to_openai(block)
                    if converted is None:
                        converted = {
                            "type": "text",
                            "text": json.dumps(block, ensure_ascii=False),
                        }
                    if converted is not None:
                        ordinary_blocks.append(converted)
                if ordinary_blocks:
                    messages.append({
                        "role": "user",
                        "content": _openai_content_value(ordinary_blocks),
                    })
                continue

            blocks: list[dict[str, Any]] = []
            for block in content:
                converted = _anthropic_block_to_openai(block)
                if converted is None:
                    converted = {
                        "type": "text",
                        "text": json.dumps(block, ensure_ascii=False),
                    }
                blocks.append(converted)
            messages.append({"role": role, "content": _openai_content_value(blocks)})
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
    "function_call": "tool_use",
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
    raw_content = message.get("content") or ""

    content: list[dict[str, Any]] = []
    if isinstance(raw_content, str):
        if raw_content:
            content.append({"type": "text", "text": raw_content})
    elif isinstance(raw_content, list):
        for block in raw_content:
            if not isinstance(block, dict):
                content.append({"type": "text", "text": str(block)})
                continue
            block_type = block.get("type")
            if block_type in {"text", "output_text"} and isinstance(block.get("text"), str):
                content.append({"type": "text", "text": block["text"]})
            else:
                # There is no lossless OpenAI→Anthropic mapping for every
                # provider-specific output block. Keep it inspectable instead
                # of silently discarding it.
                content.append({"type": "text", "text": json.dumps(block, ensure_ascii=False)})
    elif raw_content:
        content.append({"type": "text", "text": str(raw_content)})

    # Convert tool_calls if present.  Keep the legacy singular
    # ``function_call`` shape working even when this pure translator is used
    # without the gateway's response normalizer.
    raw_tool_calls = message.get("tool_calls")
    if raw_tool_calls is None and message.get("function_call") is not None:
        raw_tool_calls = [message["function_call"]]
    for index, tc in enumerate(raw_tool_calls or []):
        if not isinstance(tc, dict):
            continue
        func = tc.get("function") or tc
        if not isinstance(func, dict):
            func = {}
        arguments = func.get("arguments", "{}")
        if arguments and not isinstance(arguments, str):
            arguments_str = json.dumps(arguments, ensure_ascii=False)
        else:
            arguments_str = arguments or "{}"
        try:
            input_payload = json.loads(arguments_str)
        except json.JSONDecodeError:
            # Anthropic requires an object for tool_use.input. Preserve the
            # malformed provider payload under a visible key rather than
            # replacing it with an empty object.
            input_payload = {"raw": arguments_str}
        raw_id = str(tc.get("id") or tc.get("call_id") or "").strip()
        tool_id = raw_id or f"toolu_{secrets.token_hex(8)}"
        if raw_id and not raw_id.startswith("toolu_"):
            # Keep a valid Anthropic-looking id while retaining the upstream
            # call id, which is useful when a client sends the result back.
            tool_id = f"toolu_{raw_id}"
        content.append({
            "type": "tool_use",
            "id": tool_id,
            "name": func.get("name", ""),
            "input": input_payload,
        })

    openai_finish = result.get("choices", [{}])[0].get("finish_reason", "stop")
    stop_reason = _STOP_REASON_MAP.get(openai_finish, "end_turn")

    openai_usage = result.get("usage", {})
    if not isinstance(openai_usage, dict):
        openai_usage = {}
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
            "input_tokens": int(
                openai_usage.get("prompt_tokens")
                or openai_usage.get("input_tokens")
                or 0
            ),
            "output_tokens": int(
                openai_usage.get("completion_tokens")
                or openai_usage.get("output_tokens")
                or 0
            ),
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
_STREAM_KEY_OPEN_BLOCKS = "open_blocks"
_STREAM_KEY_NEXT_BLOCK_INDEX = "next_block_index"
_STREAM_KEY_TOOL_BLOCKS = "tool_blocks"


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
        # Anthropic streams use one content-block lifecycle per text/tool
        # block. These maps let a Chat Completions tool-call index remain
        # stable across argument fragments and parallel calls.
        _STREAM_KEY_OPEN_BLOCKS: {},
        _STREAM_KEY_NEXT_BLOCK_INDEX: 0,
        _STREAM_KEY_TOOL_BLOCKS: {},
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


def _message_start_frame(
    state: dict[str, Any],
    *,
    with_text_block: bool = True,
) -> bytes:
    """Build the message start and optionally its initial text block."""
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
    frames = [_sse_frame("message_start", payload)]
    open_blocks = state.setdefault(_STREAM_KEY_OPEN_BLOCKS, {})
    if with_text_block:
        start_block = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }
        frames.append(_sse_frame("content_block_start", start_block))
        open_blocks[0] = "text"
        state[_STREAM_KEY_NEXT_BLOCK_INDEX] = 1
    else:
        state[_STREAM_KEY_NEXT_BLOCK_INDEX] = 0
    return b"".join(frames)


def _text_delta_frame(text: str, *, index: int = 0) -> bytes:
    payload = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }
    return _sse_frame("content_block_delta", payload)


def _content_block_stop_frame(index: int) -> bytes:
    return _sse_frame(
        "content_block_stop",
        {"type": "content_block_stop", "index": index},
    )


def _tool_block_start_frame(
    index: int,
    *,
    call_id: str,
    name: str,
) -> bytes:
    return _sse_frame(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": {},
            },
        },
    )


def _tool_input_delta_frame(index: int, arguments: str) -> bytes:
    return _sse_frame(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {
                "type": "input_json_delta",
                "partial_json": arguments,
            },
        },
    )


def _closing_sequence(state: dict[str, Any]) -> bytes:
    """Build content-block stops followed by message close events."""
    open_blocks = state.setdefault(_STREAM_KEY_OPEN_BLOCKS, {0: "text"})
    stop_frames = [
        _content_block_stop_frame(index)
        for index in sorted(open_blocks)
    ]
    open_blocks.clear()
    delta_payload = {
        "type": "message_delta",
        "delta": {
            "stop_reason": state[_STREAM_KEY_LAST_STOP_REASON],
            "stop_sequence": None,
        },
        "usage": {"output_tokens": state[_STREAM_KEY_OUTPUT_TOKENS]},
    }
    stop_payload = {"type": "message_stop"}
    return b"".join([
        *stop_frames,
        _sse_frame("message_delta", delta_payload),
        _sse_frame("message_stop", stop_payload),
    ])


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

    choices = chunk.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    delta = choice.get("delta") or {}
    if not isinstance(delta, dict):
        delta = {}

    # First call → emit message_start before any content. A tool-only first
    # chunk starts directly with a tool_use block so clients never receive a
    # misleading empty text block before the function call.
    out: list[bytes] = []
    if not state[_STREAM_KEY_STARTED]:
        state[_STREAM_KEY_STARTED] = True
        has_tool_calls = isinstance(delta.get("tool_calls"), list) and bool(delta.get("tool_calls"))
        has_text = isinstance(delta.get("content"), str) and bool(delta.get("content"))
        out.append(_message_start_frame(
            state,
            with_text_block=not (has_tool_calls and not has_text),
        ))

    if choices:
        content_text = delta.get("content")
        finish_reason = choice.get("finish_reason")

        if isinstance(content_text, str) and content_text:
            open_blocks = state.setdefault(_STREAM_KEY_OPEN_BLOCKS, {})
            text_indices = [
                index for index, block_type in open_blocks.items()
                if block_type == "text"
            ]
            if text_indices:
                text_index = text_indices[0]
            else:
                text_index = state.get(_STREAM_KEY_NEXT_BLOCK_INDEX, 0)
                state[_STREAM_KEY_NEXT_BLOCK_INDEX] = text_index + 1
                open_blocks[text_index] = "text"
                out.append(_sse_frame(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": text_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                ))
            state[_STREAM_KEY_OUTPUT_TOKENS] += max(1, len(content_text) // 4)
            out.append(_text_delta_frame(content_text, index=text_index))

        raw_tool_calls = delta.get("tool_calls")
        if not raw_tool_calls and isinstance(delta.get("function_call"), dict):
            # Older OpenAI-compatible providers still stream the singular
            # function_call shape. Normalize it here as well as in the gateway
            # SSE sanitizer so direct registry users do not lose tool calls.
            legacy = dict(delta["function_call"])
            call_id = str(
                legacy.pop("id", None)
                or delta.get("tool_call_id")
                or f"call_legacy_{choice.get('index', 0)}"
            ).strip()
            raw_tool_calls = [{
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": legacy,
            }]
        if isinstance(raw_tool_calls, list) and raw_tool_calls:
            open_blocks = state.setdefault(_STREAM_KEY_OPEN_BLOCKS, {})
            # Text blocks must close before Anthropic starts a tool_use block.
            for index, block_type in list(open_blocks.items()):
                if block_type == "text":
                    out.append(_content_block_stop_frame(index))
                    del open_blocks[index]

            tool_blocks = state.setdefault(_STREAM_KEY_TOOL_BLOCKS, {})
            state[_STREAM_KEY_LAST_STOP_REASON] = "tool_use"
            try:
                choice_index = int(choice.get("index", 0))
            except (TypeError, ValueError):
                choice_index = 0
            for position, raw_call in enumerate(raw_tool_calls):
                if not isinstance(raw_call, dict):
                    continue
                try:
                    call_index = int(raw_call.get("index", position))
                except (TypeError, ValueError):
                    call_index = position
                call_key = (choice_index, call_index)
                function = raw_call.get("function") or {}
                if not isinstance(function, dict):
                    function = {}
                block_index = tool_blocks.get(call_key)
                if block_index is None:
                    block_index = state.get(_STREAM_KEY_NEXT_BLOCK_INDEX, 0)
                    state[_STREAM_KEY_NEXT_BLOCK_INDEX] = block_index + 1
                    call_id = str(
                        raw_call.get("id")
                        or raw_call.get("call_id")
                        or f"toolu_{secrets.token_hex(8)}"
                    )
                    name = str(function.get("name") or "")
                    tool_blocks[call_key] = block_index
                    open_blocks[block_index] = "tool"
                    out.append(_tool_block_start_frame(
                        block_index,
                        call_id=call_id,
                        name=name,
                    ))
                arguments = function.get("arguments")
                if arguments is not None and arguments != "":
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    out.append(_tool_input_delta_frame(block_index, arguments))

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
