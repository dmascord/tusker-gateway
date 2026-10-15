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
        conversation_id, metrics_registry, request_id, pool,
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


def _leaked_opener_frames() -> list[bytes]:
    """Upstream bytes captured from the failing 2026-10-15 alibaba turn.

    ``deepseek-v4.1-flash`` streamed the literal DSML opener ``<tool_calls`` as
    content (split across three deltas), stopped without any structured call,
    and the gateway's stripper swallowed the markup, leaving the client with
    nothing but a turn whose verdict was ``reasoning_only_or_empty``.
    """
    return [
        _sse({"id": "chatcmpl-leak", "object": "chat.completion.chunk", "choices": [{
            "index": 0,
            "delta": {"content": "", "reasoning_content": "", "role": "assistant"},
            "finish_reason": None,
        }]}),
        _sse({"id": "chatcmpl-leak", "choices": [{
            "index": 0, "delta": {"content": "<tool", "reasoning_content": ""},
            "finish_reason": None,
        }]}),
        _sse({"id": "chatcmpl-leak", "choices": [{
            "index": 0, "delta": {"content": "_c", "reasoning_content": ""},
            "finish_reason": None,
        }]}),
        _sse({"id": "chatcmpl-leak", "choices": [{
            "index": 0, "delta": {"content": "alls", "reasoning_content": ""},
            "finish_reason": None,
        }]}),
        _sse({"id": "chatcmpl-leak", "choices": [{
            "index": 0, "delta": {"content": "", "reasoning_content": ""},
            "finish_reason": "stop",
        }]}),
        b"data: [DONE]\n\n",
    ]


def _usable_tool_call_frames(call_id: str, arguments: str) -> list[bytes]:
    return [
        _sse({"id": "chatcmpl-good", "choices": [{
            "index": 0, "delta": {"role": "assistant", "content": ""},
            "finish_reason": None,
        }]}),
        _sse({"id": "chatcmpl-good", "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": {"name": "bash", "arguments": arguments},
            }]},
            "finish_reason": None,
        }]}),
        _sse({"id": "chatcmpl-good", "choices": [{
            "index": 0, "delta": {}, "finish_reason": "tool_calls",
        }]}),
        b"data: [DONE]\n\n",
    ]


@pytest.mark.asyncio
async def test_unusable_tool_turns_fail_over_before_client_output(
    client, app, caplog,
):
    """Several unusable tool turns in a row must not reach the client.

    Rejected turns carry no client-visible output, so the stream can still be
    re-dispatched. With a single retry a pool that serves two leaked-opener
    turns in a row answered the client with the "route was temporarily
    quarantined" banner; the retry budget now covers three alternates.
    """
    from unittest.mock import MagicMock

    command = "ls -la /tmp\n"
    arguments = json.dumps({"command": command})
    handled: list[tuple[str, str]] = []

    async def fake_chat(
        _client, provider, model, messages, *, stream, tools, tool_choice, extra_body,
        conversation_id, metrics_registry, request_id, pool,
    ):
        handled.append((provider, model))

        async def upstream_stream():
            frames = (
                _leaked_opener_frames()
                if provider != "usable"
                else _usable_tool_call_frames("call_after_failover", arguments)
            )
            for frame in frames:
                yield frame

        return upstream_stream()

    pool_manager = MagicMock()
    pool_manager.fallback_pools.return_value = ()
    pool_manager.select.side_effect = [
        ("leaky-one", "deepseek-v4.1-flash"),
        ("leaky-two", "glm-5.3-flash"),
        ("usable", "gemini-3.5-flash"),
    ]
    app["pool_manager"] = pool_manager

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new=fake_chat):
        with caplog.at_level(logging.WARNING, logger="tusker_gateway.endpoints"):
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "hermes-code",
                    "messages": [{"role": "user", "content": "list /tmp"}],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "parameters": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                            },
                        },
                    }],
                    "stream": True,
                },
                headers=HEADERS_AUTH,
            )
            body = await response.read()

    assert response.status == 200
    assert [provider for provider, _ in handled] == ["leaky-one", "leaky-two", "usable"]
    assert b"temporarily quarantined" not in body, body[-400:]
    assert b"<tool_calls" not in body, body[-400:]

    emitted_arguments = "".join(
        call["function"].get("arguments", "")
        for event in _read_sse_data(body)
        for choice in event.get("choices", [])
        for call in (choice.get("delta", {}) or {}).get("tool_calls", [])
    )
    assert json.loads(emitted_arguments)["command"] == command

    fallbacks = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("stream pre-visible fallback")
    ]
    assert len(fallbacks) == 2
    assert "attempt=1" in fallbacks[0]
    assert "attempt=2" in fallbacks[1]
