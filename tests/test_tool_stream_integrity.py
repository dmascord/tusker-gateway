"""End-to-end regression harness for large tool payload preservation.

Run with:
    pytest tests/test_tool_stream_integrity.py -q

The provider is simulated, while the request and streamed response pass through
the public chat-completions handler, including tool-stream preflight and SSE
serialization. No shell command is executed.
"""
from __future__ import annotations

import hashlib
import json
import logging
from unittest.mock import patch

import pytest

from .conftest import HEADERS_AUTH


def _sse(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def _read_sse_data(body: bytes) -> list[dict]:
    events = []
    for event in body.split(b"\n\n"):
        if event.startswith(b"data: ") and event[6:] != b"[DONE]":
            events.append(json.loads(event[6:]))
    return events


@pytest.mark.asyncio
async def test_large_bash_tool_turn_is_preserved_end_to_end(
    client, app, monkeypatch, caplog,
):
    """Retain every byte of long tool history and fragmented Bash arguments."""
    monkeypatch.setenv("TUSKER_TOOL_DIAGNOSTICS", "true")

    # Distinct line numbers, punctuation, tabs, Unicode, and a final newline
    # make truncation, duplication, normalization, or newline damage visible.
    tool_output = "".join(
        f"line-{i:05d}\tvalue={i * 17} | Ω-{i % 13}\n" for i in range(2400)
    )
    command = "cat <<'PAYLOAD'\n" + tool_output + "PAYLOAD\n"
    arguments = json.dumps({"command": command}, ensure_ascii=False, separators=(",", ":"))
    call_id = "call_integrity_long_bash"
    history = [
        {"role": "user", "content": "Inspect the generated output."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "bash", "arguments": arguments},
            }],
        },
        {"role": "tool", "tool_call_id": call_id, "content": tool_output},
    ]
    forwarded = {}

    async def fake_chat(
        _client, provider, model, messages, *, stream, tools, tool_choice, extra_body,
        conversation_id, metrics_registry,
    ):
        forwarded.update(
            provider=provider,
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )

        async def upstream_stream():
            # Split arguments in irregular fragments, including inside JSON
            # string escapes and immediately around newline boundaries.
            cuts = (1, 19, 127, 511, 2047, 4093, 8191, len(arguments))
            start = 0
            for index, end in enumerate(cuts):
                end = min(end, len(arguments))
                if end <= start:
                    continue
                function_delta = {"arguments": arguments[start:end]}
                if index == 0:
                    function_delta["name"] = "bash"
                tool_delta = {"index": 0, "function": function_delta}
                if index == 0:
                    tool_delta.update(id=call_id, type="function")
                yield _sse({"id": "chatcmpl-integrity", "choices": [{
                    "index": 0,
                    "delta": {"tool_calls": [tool_delta]},
                    "finish_reason": None,
                }]})
                start = end
            if start < len(arguments):
                yield _sse({"id": "chatcmpl-integrity", "choices": [{
                    "index": 0,
                    "delta": {"tool_calls": [{
                        "index": 0, "function": {"arguments": arguments[start:]},
                    }]},
                    "finish_reason": None,
                }]})
            yield _sse({"id": "chatcmpl-integrity", "choices": [{
                "index": 0, "delta": {}, "finish_reason": "tool_calls",
            }]})
            yield b"data: [DONE]\n\n"

        return upstream_stream()

    tool_schema = [{
        "type": "function",
        "function": {
            "name": "bash",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
        },
    }]
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new=fake_chat):
        with caplog.at_level(logging.INFO, logger="tusker_gateway.endpoints"):
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "synthetic::syn:large:text",
                    "messages": history,
                    "tools": tool_schema,
                    "tool_choice": "required",
                    "stream": True,
                },
                headers=HEADERS_AUTH,
            )
            body = await response.read()

    assert response.status == 200
    assert forwarded["messages"] == history
    assert forwarded["tools"] == tool_schema
    assert forwarded["tool_choice"] == "required"
    assert forwarded["provider"] == "synthetic"

    events = _read_sse_data(body)
    emitted_args = "".join(
        call["function"].get("arguments", "")
        for event in events
        for choice in event.get("choices", [])
        for call in (choice.get("delta", {}) or {}).get("tool_calls", [])
    )
    assert emitted_args == arguments
    assert json.loads(emitted_args)["command"] == command
    assert len(tool_output.splitlines()) == 2400
    assert body.count(b"data: ") >= 10

    digest = hashlib.sha256(arguments.encode("utf-8")).hexdigest()
    output_digest = hashlib.sha256(tool_output.encode("utf-8")).hexdigest()
    preflight = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("tool stream preflight")
    ]
    assert len(preflight) == 1
    assert f"sha256={digest}" in preflight[0]
    assert f"utf8={len(arguments.encode('utf-8'))}" in preflight[0]
    history_diagnostics = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("tool history integrity")
    ]
    assert len(history_diagnostics) == 1
    assert f"sha256={digest}" in history_diagnostics[0]
    assert f"sha256={output_digest}" in history_diagnostics[0]
