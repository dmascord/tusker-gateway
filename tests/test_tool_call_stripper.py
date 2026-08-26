"""Tests for the XML/Markdown tool-call stripper in endpoints.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from .conftest import HEADERS_AUTH

from tusker_gateway.endpoints import _ToolCallStripper, _strip_xml_tool_calls


def test_stripper_complete_block_in_single_chunk():
    s = _ToolCallStripper()
    out = s.feed(
        "hello <tool_call>\n<function=bash>\n<parameter=command>ls</parameter>\n</function>\n</tool_call> world"
    )
    assert "tool_call" not in out
    assert "hello" in out and "world" in out


def test_stripper_block_split_across_chunks():
    s = _ToolCallStripper()
    # First chunk has only partial markup. Second chunk finishes it.
    out1 = s.feed("start <tool_c")
    out2 = s.feed(
        "all>\n<function=bash>\n<parameter=command>ls</parameter>\n</function>\n</tool_call> end"
    )
    combined = out1 + out2
    assert "tool_call" not in combined
    assert "<function" not in combined
    assert "start" in combined
    assert "end" in combined


def test_stripper_preserves_normal_prose_with_word_tool_call():
    s = _ToolCallStripper()
    # Plain text containing the *word* "tool_call" should be preserved.
    out = s.feed("the tool_call protocol is used for function execution")
    assert out == "the tool_call protocol is used for function execution"


def test_stripper_only_function_inline_block():
    s = _ToolCallStripper()
    out = s.feed("before <function=name>foo</function> after")
    assert out == "before  after"


def test_stripper_multiple_tool_call_blocks():
    s = _ToolCallStripper()
    out = s.feed(
        "a<tool_call><function=f1>x</function></tool_call>b<tool_call><function=f2>y</function></tool_call>c"
    )
    assert "tool_call" not in out
    assert "function" not in out
    assert "abc" == out


def test_strip_xml_tool_calls_helper_handles_plain_text():
    # The helper should leave plain text untouched.
    assert _strip_xml_tool_calls("hello world") == "hello world"
    assert _strip_xml_tool_calls("") == ""


def test_strip_xml_tool_calls_helper_strips_full_block():
    src = "pre <tool_call>\n<function=f>\n</function>\n</tool_call> post"
    out = _strip_xml_tool_calls(src)
    assert "tool_call" not in out
    assert "<function" not in out
    assert "pre" in out and "post" in out


@pytest.mark.asyncio
async def test_stream_normalizer_strips_inline_tool_call_text(client):
    """The streaming endpoint must strip XML tool calls from content deltas."""
    # Simulate a model that emits raw text tool_call markup in the content.
    leaked = (
        "Let me check the logs.\n\n"
        "<tool_call>\n"
        "<function=bash>\n"
        "<parameter=command>ssh wildduck cat /etc/fail2ban/filter.d/haraka-rcpt-probe-v3.conf</parameter>\n"
        "<parameter=timeout>10</parameter>\n"
        "</function>\n"
        "</tool_call>\n\n"
        "That should help.\n"
    )

    async def leaked_stream(*args, **kwargs):
        # One chunk with content; another with the leaked tool-call text.
        yield (
            b'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
            b'"content":"Let me check the logs.\\n\\n"},"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"<tool_call>\\n<function=bash>\\n<parameter=command>ssh wildduck...</parameter>\\n<parameter=timeout>10</parameter>\\n</function>\\n</tool_call>\\n\\nThat should help.\\n"},'
            b'"finish_reason":"stop"}]}\n\n'
        )

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = leaked_stream()
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "check fail2ban"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        body = await resp.read()

    # The streamed content must NOT contain the leaked tool_call markup.
    text = body.decode("utf-8", errors="replace")
    # There may be multiple JSON frames; concatenate their delta.content.
    import json as _json
    leaked_present = False
    prose_present = False
    for line in text.splitlines():
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        try:
            obj = _json.loads(line[len("data: "):])
        except Exception:
            continue
        for ch in obj.get("choices", []):
            content = ch.get("delta", {}).get("content") or ""
            if "tool_call" in content or "<function" in content:
                leaked_present = True
            if "Let me check the logs" in content:
                prose_present = True

    assert not leaked_present, f"tool_call markup leaked into stream: {text!r}"
    assert prose_present, f"expected prose to survive: {text!r}"
    assert b"data: [DONE]" in body


@pytest.mark.asyncio
async def test_stream_normalizer_promotes_reasoning_content(client):
    """reasoning-only models must not produce empty-text turns for OMP.

    Some upstream reasoning models (e.g. qwen) emit their thinking in
    `reasoning_content` while leaving `content` null. OMP treats a turn with
    content=null as "no text" and ends early. The normalizer must promote
    `reasoning_content` into `content` so the client sees real text.
    """
    async def reasoning_stream(*args, **kwargs):
        yield (
            b'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
            b'"content":null,"reasoning_content":"Let me think about this"}}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":null,"reasoning_content":" for a moment."},"finish_reason":"stop"}]}\n\n'
        )

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = reasoning_stream()
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "say hi"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        body = await resp.read()

    text = body.decode("utf-8", errors="replace")
    # The reasoning text must be promoted into content so the client (OMP)
    # sees real text instead of an empty turn that finishes early.
    assert '"content": "Let me think about this"' in text
    assert '"content": " for a moment."' in text
    # content must never be null in the promoted deltas.
    assert '"content": null' not in text


@pytest.mark.asyncio
async def test_stream_normalizer_emits_tool_calls_for_bare_function_blocks(client):
    """Models that emit bare <function=name>...</function> (no <​tool_call> wrapper)
    across multiple SSE deltas must not leak the XML into content and must
    emit a structured delta.tool_calls frame so OMP picks it up.

    Regression for the OMP-reported bug where raw
        <function=bash>
        <parameter=command>...</parameter>
        <parameter=timeout>15</parameter>
        </function>
    appeared in the streamed `content` field and no `tool_calls` was ever
    surfaced, so OMP rendered the markup as visible text and never invoked
    the tool.
    """
    # Each chunk is one SSE event's worth of delta.content from the upstream.
    async def leaked_stream(*args, **kwargs):
        yield (
            b'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
            b'"content":"I will run that.\\n\\n"},"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"<function=bash>"},"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"\\n<parameter=command>ssh wildduck cat /etc/sender.toml</parameter>"},'
            b'"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"\\n<parameter=timeout>15</parameter>"},'
            b'"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"\\n</function>"},"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{},'
            b'"finish_reason":"stop"}]}\n\n'
        )
        yield b'data: [DONE]\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = leaked_stream()
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "check sender config"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        body = await resp.read()

    text = body.decode("utf-8", errors="replace")
    import json as _json

    leaked_present = False
    prose_present = False
    tool_calls_seen: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        try:
            obj = _json.loads(line[len("data: "):])
        except Exception:
            continue
        for ch in obj.get("choices", []):
            delta = ch.get("delta", {}) or {}
            content = delta.get("content") or ""
            if "<function" in content or "tool_call" in content or "<parameter=" in content:
                leaked_present = True
            if "I will run that" in content:
                prose_present = True
            tc = delta.get("tool_calls")
            if isinstance(tc, list):
                for item in tc:
                    if isinstance(item, dict):
                        tool_calls_seen.append(item)

    # (1) Markup must NOT leak into the streamed content.
    assert not leaked_present, f"tool-call XML leaked into stream: {text!r}"
    # (2) Prose around the tool-call block must survive.
    assert prose_present, f"prose around tool call missing from stream: {text!r}"
    # (3) A structured tool_calls delta must be emitted with the parsed name + args.
    assert tool_calls_seen, f"no delta.tool_calls emitted: {text!r}"
    fn = tool_calls_seen[0].get("function") or {}
    assert fn.get("name") == "bash", f"unexpected tool name: {tool_calls_seen!r}"
    args_raw = fn.get("arguments")
    if isinstance(args_raw, str):
        args = _json.loads(args_raw)
    elif isinstance(args_raw, dict):
        args = args_raw
    else:
        args = {}
    assert args.get("command") == "ssh wildduck cat /etc/sender.toml"
    assert str(args.get("timeout")) == "15"


def test_stripper_handles_bare_function_block_split_across_many_chunks():
    """The bare <function=name>...</function> shape (no <​tool_call> wrapper) must
    be stripped even when its opener, every <parameter=...> line, and the
    closing </function> arrive in separate stream chunks."""
    s = _ToolCallStripper()
    chunks = [
        "I will run that.\n\n",
        "<function=bash>",
        "\n<parameter=command>ssh wildduck cat /etc/sender.toml</parameter>",
        "\n<parameter=timeout>15</parameter>",
        "\n</function>",
    ]
    combined = "".join(s.feed(c) for c in chunks) + s.flush()
    assert "<function" not in combined, f"function block leaked: {combined!r}"
    assert "<parameter=" not in combined, f"parameter block leaked: {combined!r}"
    assert "I will run that" in combined


@pytest.mark.asyncio
async def test_stream_normalizer_injects_finish_when_upstream_omits_it(client):
    """If the upstream stream ends without a finish_reason chunk, the gateway
    must synthesize one so OMP doesn't report
    'stream closed before a finish_reason was received'.
    """
    async def no_finish_stream(*args, **kwargs):
        # Upstream emits content but never sends finish_reason — simulates
        # a provider that drops the connection after content.
        yield (
            b'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
            b'"content":"Hello world"}}]}\n\n'
        )
        # Stream ends here — no finish_reason chunk, no [DONE].

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = no_finish_stream()
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "say hi"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        body = await resp.read()

    text = body.decode("utf-8", errors="replace")
    # Content must be present.
    assert '"content": "Hello world"' in text
    # A finish_reason: "stop" chunk must be present even though the
    # upstream never sent one.
    assert '"finish_reason": "stop"' in text
    # [DONE] sentinel must still be present.
    assert b"data: [DONE]" in body
