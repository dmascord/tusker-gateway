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
import pathlib
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


def _reasoning_only_length_frames() -> list[bytes]:
    """The upstream bytes of the failing 2026-11-07 DeepSeek V4 turn.

    Captured verbatim from the production turn-capture store (capture
    ``c16bddd22ee54ceb926fcad161a10a9a``, provider
    ``synthetic/hf:deepseek-ai/DeepSeek-V4.1-Flash-1017``) and stored as
    ``tests/fixtures/reasoning_only_length_turn.sse``.

    The model streamed 24 reasoning deltas and then stopped with
    ``finish_reason=length`` without emitting a single visible content
    character or tool call. The upstream usage frame reports
    ``completion_tokens: 120`` with ``reasoning_tokens: 120``: the client's
    120-token caption cap was spent entirely on reasoning. This is a
    no-tools caption turn, matching the captured request.
    """
    fixture = (
        pathlib.Path(__file__).parent
        / "fixtures"
        / "reasoning_only_length_turn.sse"
    )
    raw = fixture.read_bytes()
    frames = [
        part if part.startswith(b"data: ") else b"data: " + part
        for part in (chunk.strip() for chunk in raw.split(b"\n\n"))
        if part
    ]
    return [frame + b"\n\n" for frame in frames]


@pytest.mark.asyncio
async def test_reasoning_only_length_turn_never_reaches_the_client_as_length(
    client, app, caplog,
):
    """A reasoning-only ``length`` turn must not reach the client as ``length``.

    The 2026-11-07 incident: a reasoning-heavy candidate burned the client's
    whole 120-token caption budget on reasoning and stopped with
    ``finish_reason=length`` and no text. The client received that terminal,
    discarded the turn as "nothing delivered", and retried the identical
    request three times before raising "Length-stop recovery gave up".

    The gateway must reject the turn before the terminal frame is yielded, so
    the client sees an actionable error instead of a "retry the same thing"
    signal. A ``length`` terminal must never reach the client: it only starts
    a retry loop that cannot succeed.

    The route is not re-dispatched here: the reasoning bytes already reached
    the client, so the "never splice providers after visible output" invariant
    still holds. What changes is the terminal the client sees.
    """
    from unittest.mock import MagicMock

    handled: list[tuple[str, str]] = []
    budgets: list[dict] = []

    async def fake_chat(
        _client, provider, model, messages, *, stream, tools, tool_choice, extra_body,
        conversation_id, metrics_registry, request_id, pool,
    ):
        handled.append((provider, model))
        budgets.append(dict(extra_body or {}))

        async def upstream_stream():
            for frame in _reasoning_only_length_frames():
                yield frame

        return upstream_stream()

    pool_manager = MagicMock()
    pool_manager.fallback_pools.return_value = ()
    pool_manager.select.side_effect = [
        ("synthetic", "hf:deepseek-ai/DeepSeek-V4.1-Flash-1017"),
    ]
    app["pool_manager"] = pool_manager

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new=fake_chat):
        with caplog.at_level(logging.WARNING, logger="tusker_gateway.endpoints"):
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "hermes-code",
                    "messages": [{"role": "user", "content": "caption this"}],
                    "max_tokens": 120,
                    "stream": True,
                },
                headers=HEADERS_AUTH,
            )
            body = await response.read()

    assert response.status == 200
    assert handled == [("synthetic", "hf:deepseek-ai/DeepSeek-V4.1-Flash-1017")]

    # The reasoning floor lifts the client's 120-token cap so the model has
    # room for an answer after thinking.
    assert budgets[0].get("max_tokens") == 2000, budgets

    finish_reasons = [
        choice.get("finish_reason")
        for event in _read_sse_data(body)
        for choice in event.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert "length" not in finish_reasons, finish_reasons
    assert finish_reasons == ["stop"], finish_reasons
    assert b"spent its whole output budget on reasoning" in body, body[-400:]


def _thinking_only_length_frames(text: str) -> list[bytes]:
    """Upstream bytes of a MiniMax-style turn that never leaves its thinking.

    MiniMax-M3 streams its chain of thought in the OpenAI ``content``
    channel, wrapped in ``<think>``. When the output budget runs out inside
    the thinking, the tag is never closed and no visible answer follows.
    OMP heals the leak into a thinking block, so the client is left with
    nothing to show.
    """
    return [
        _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": text}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
    ]


async def _post_stream(client, app, *, provider: str, model: str, frames):
    """Drive one streaming request through the public handler."""
    from unittest.mock import MagicMock

    handled: list[tuple[str, str]] = []

    async def fake_chat(
        _client, provider, model, messages, *, stream, tools, tool_choice, extra_body,
        conversation_id, metrics_registry, request_id, pool,
    ):
        handled.append((provider, model))

        async def upstream_stream():
            for frame in frames:
                yield frame

        return upstream_stream()

    pool_manager = MagicMock()
    pool_manager.fallback_pools.return_value = ()
    pool_manager.select.side_effect = [(provider, model)]
    app["pool_manager"] = pool_manager

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new=fake_chat):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "explain the stack"}],
                "max_tokens": 16384,
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        body = await response.read()
    return response, body, handled


@pytest.mark.asyncio
async def test_inband_thinking_length_turn_never_reaches_the_client_as_length(
    client, app, caplog,
):
    """A turn that only ever produced thinking must not reach the client.

    The 2026-11-08 recurrence: MiniMax-M3 spent its whole 16384-token
    budget inside a ``<think>`` block and stopped with
    ``finish_reason=length``. OMP healed the leaked thinking into a thinking
    block, saw no text and no tool call, and retried the identical request
    three times before raising "Length-stop recovery gave up".

    The gateway must reject the turn before the terminal frame is yielded so
    the client sees an actionable error instead of a retry signal.
    """
    thinking = "<think>" + ("reasoning that never ends. " * 200)
    response, body, handled = await _post_stream(
        client,
        app,
        provider="minimax",
        model="MiniMax-M3",
        frames=_thinking_only_length_frames(thinking),
    )

    assert response.status == 200
    assert handled == [("minimax", "MiniMax-M3")]

    finish_reasons = [
        choice.get("finish_reason")
        for event in _read_sse_data(body)
        for choice in event.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert "length" not in finish_reasons, finish_reasons
    assert b"spent its whole output budget on reasoning" in body, body[-400:]


@pytest.mark.asyncio
async def test_visible_text_length_turn_is_delivered_to_the_client(
    client, app,
):
    """A truncated turn that did deliver text must reach the client.

    The deployed guard of 2026-11-08 rejected any ``length`` terminal whose
    own frame carried less than ten characters, which threw away a MiniMax
    turn that had already delivered 57k characters of answer. The client
    keeps a truncated turn it can show, so the gateway must not reject it.
    """
    answer = "The stack order is ingress to egress. " * 40
    response, body, handled = await _post_stream(
        client,
        app,
        provider="minimax",
        model="MiniMax-M3",
        frames=[
            _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
            _sse({"choices": [{"index": 0, "delta": {"content": answer}}]}),
            _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
        ],
    )

    assert response.status == 200
    assert handled == [("minimax", "MiniMax-M3")]

    finish_reasons = [
        choice.get("finish_reason")
        for event in _read_sse_data(body)
        for choice in event.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert finish_reasons == ["length"], finish_reasons
    assert answer.encode() in body, body[-400:]


@pytest.mark.asyncio
async def test_thinking_followed_by_an_answer_length_turn_is_delivered(
    client, app,
):
    """Thinking followed by a real answer stays deliverable after a cut."""
    frames = [
        _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": "<think>reasoning</think>"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": "The answer is 42."}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
    ]
    response, body, handled = await _post_stream(
        client,
        app,
        provider="minimax",
        model="MiniMax-M3",
        frames=frames,
    )

    assert response.status == 200
    finish_reasons = [
        choice.get("finish_reason")
        for event in _read_sse_data(body)
        for choice in event.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert finish_reasons == ["length"], finish_reasons
    assert b"The answer is 42." in body, body[-400:]


@pytest.mark.asyncio
async def test_prefilled_template_reasoning_turn_is_rejected(
    client, app,
):
    """Reasoning behind a prefilled opener, cut off before any answer.

    DeepSeek-R1, Qwen3-Thinking and friends get the ``<think>`` opener
    prefilled into the prompt, so the model streams the reasoning and then
    the bare ``</think>`` that closes it. When the output budget runs out
    before the answer, the whole turn is reasoning and OMP reclassifies the
    single text block as a thinking block, finds nothing deliverable, and
    retries the identical request three times.

    The gateway must reject the turn with an actionable error instead of
    handing over a ``length`` terminal.
    """
    frames = [
        _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": "reasoning that never ends. " * 50}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": "</think>"}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
    ]
    response, body, handled = await _post_stream(
        client,
        app,
        provider="minimax",
        model="MiniMax-M3",
        frames=frames,
    )

    assert response.status == 200
    finish_reasons = [
        choice.get("finish_reason")
        for event in _read_sse_data(body)
        for choice in event.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert "length" not in finish_reasons, finish_reasons
    assert b"spent its whole output budget on reasoning" in body, body[-400:]


@pytest.mark.asyncio
async def test_bare_close_followed_by_an_answer_is_delivered(
    client, app,
):
    """A bare ``</think>`` with visible text after it stays deliverable."""
    frames = [
        _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": "</think>"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": "The answer is 42."}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
    ]
    response, body, handled = await _post_stream(
        client,
        app,
        provider="minimax",
        model="MiniMax-M3",
        frames=frames,
    )

    assert response.status == 200
    finish_reasons = [
        choice.get("finish_reason")
        for event in _read_sse_data(body)
        for choice in event.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert finish_reasons == ["length"], finish_reasons
    assert b"The answer is 42." in body, body[-400:]


@pytest.mark.asyncio
async def test_short_answer_length_turn_is_delivered(
    client, app,
):
    """A one-word answer must survive the length guard.

    The redaction restorer holds a trailing run of text back until the
    stream ends, so the client can receive a short answer only in the tail
    frame. The guard has to judge what the client will receive, not what a
    single frame happened to carry.
    """
    frames = [
        _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": "42"}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
    ]
    response, body, handled = await _post_stream(
        client,
        app,
        provider="minimax",
        model="MiniMax-M3",
        frames=frames,
    )

    assert response.status == 200
    finish_reasons = [
        choice.get("finish_reason")
        for event in _read_sse_data(body)
        for choice in event.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert finish_reasons == ["length"], finish_reasons
    assert b"42" in body, body[-400:]


@pytest.mark.asyncio
async def test_thinking_tag_split_across_frames_is_not_visible_text(
    client, app,
):
    """A reasoning tag split across frames must still be recognised."""
    frames = [
        _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": "<thi"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": "nk>reasoning"}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
    ]
    response, body, handled = await _post_stream(
        client,
        app,
        provider="minimax",
        model="MiniMax-M3",
        frames=frames,
    )

    assert response.status == 200
    finish_reasons = [
        choice.get("finish_reason")
        for event in _read_sse_data(body)
        for choice in event.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert "length" not in finish_reasons, finish_reasons
    assert b"spent its whole output budget on reasoning" in body, body[-400:]


@pytest.mark.asyncio
async def test_native_reasoning_keeps_the_text_visible(
    client, app,
):
    """Native reasoning next to the text means the text stays deliverable.

    OMP only reclassifies a bare closing tag when the message holds nothing
    but that one text block. A turn with native reasoning holds more, so the
    text survives and the client can use it.
    """
    frames = [
        _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"reasoning_content": "thinking"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": "The answer is 42."}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
    ]
    response, body, handled = await _post_stream(
        client,
        app,
        provider="minimax",
        model="MiniMax-M3",
        frames=frames,
    )

    assert response.status == 200
    finish_reasons = [
        choice.get("finish_reason")
        for event in _read_sse_data(body)
        for choice in event.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert finish_reasons == ["length"], finish_reasons
    assert b"The answer is 42." in body, body[-400:]
