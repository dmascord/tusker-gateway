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
