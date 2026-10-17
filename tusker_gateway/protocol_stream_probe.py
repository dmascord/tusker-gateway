"""Bounded SSE probe for OpenAI-compatible streamed tool calls.

This consumes a finite byte stream and checks that the gateway-facing
wire contract can be reconstructed, without recording response bodies.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_probe import ProbeAssessment

StreamExecutor = Callable[[dict[str, Any]], Awaitable[AsyncIterator[bytes]]]


async def probe_streaming_tool_fragments(
    *, provider: str, endpoint_id: str, model: str,
    execute: StreamExecutor, store: ProtocolEvidenceStore,
    timeout_seconds: float = 15.0, max_bytes: int = 262144,
) -> ProbeAssessment:
    if not (0 < timeout_seconds <= 60) or not (0 < max_bytes <= 1048576):
        raise ValueError("invalid streaming probe limits")
    request = {
        "model": model, "stream": True,
        "messages": [{"role": "user", "content": "Call protocol_probe_echo with value ok."}],
        "tools": [{"type": "function", "function": {
            "name": "protocol_probe_echo", "description": "Echo a value",
            "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                           "required": ["value"]},
        }}],
        "tool_choice": "required", "max_tokens": 128,
    }

    async def inspect() -> ProbeAssessment:
        iterator = await execute(request)
        buffer = b""
        total = 0
        calls: dict[int, dict[str, str]] = {}
        finished = False
        done = False
        async for chunk in iterator:
            total += len(chunk)
            if total > max_bytes:
                return ProbeAssessment("streaming_tool_fragments", "unknown", "size_limit")
            buffer += chunk.replace(b"\r\n", b"\n")
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                lines = [line[5:].strip() for line in frame.splitlines()
                         if line.startswith(b"data:")]
                if not lines:
                    continue
                payload = b"\n".join(lines)
                if payload == b"[DONE]":
                    done = True
                    continue
                try:
                    event = json.loads(payload)
                except (ValueError, UnicodeDecodeError):
                    return ProbeAssessment("streaming_tool_fragments", "unknown", "invalid_sse")
                for choice in event.get("choices", []):
                    if choice.get("finish_reason") in {"tool_calls", "stop"}:
                        finished = True
                    delta = choice.get("delta") or {}
                    for call in delta.get("tool_calls") or []:
                        index = call.get("index", 0)
                        if not isinstance(index, int) or index < 0:
                            return ProbeAssessment("streaming_tool_fragments", "unknown", "invalid_index")
                        target = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if isinstance(call.get("id"), str):
                            target["id"] += call["id"]
                        function = call.get("function") or {}
                        if isinstance(function.get("name"), str):
                            target["name"] += function["name"]
                        if isinstance(function.get("arguments"), str):
                            target["arguments"] += function["arguments"]
        valid = False
        for call in calls.values():
            try:
                arguments = json.loads(call["arguments"])
            except (ValueError, TypeError):
                continue
            valid |= bool(call["id"]) and call["name"] == "protocol_probe_echo" and (
                isinstance(arguments, dict) and arguments.get("value") == "ok"
            )
        return ProbeAssessment(
            "streaming_tool_fragments", "passed" if valid and finished and done else "unknown",
            None if valid and finished and done else "incomplete_stream",
        )

    try:
        result = await asyncio.wait_for(inspect(), timeout_seconds)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        result = ProbeAssessment("streaming_tool_fragments", "unavailable", "transport")
    store.record(provider, endpoint_id, model, result.feature, result.status)
    return result
