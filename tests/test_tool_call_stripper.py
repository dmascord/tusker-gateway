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
async def test_stream_normalizer_promotes_tool_markup_from_reasoning_field(client):
    """OpenRouter's ``reasoning`` field must not bypass tool normalization."""
    import json as _json

    async def reasoning_tool_stream(*args, **kwargs):
        for part in (
            "<dots_function_call>\n",
            '<invoke name="bash">\n'
            '<parameter name="command">ls</parameter>\n'
            '</invoke>\n</dots_function_call>',
        ):
            payload = {
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": "",
                        "reasoning": part,
                        "reasoning_details": [{"type": "reasoning.text", "text": part}],
                    },
                    "finish_reason": None,
                }],
            }
            yield f"data: {_json.dumps(payload)}\n\n".encode()
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    tools = [{
        "type": "function",
        "function": {
            "name": "bash",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }]
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = reasoning_tool_stream()
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "list files"}],
                "tools": tools,
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        body = await resp.read()

    text = body.decode("utf-8", errors="replace")
    assert "dots_function_call" not in text
    assert "<invoke" not in text
    assert "<parameter" not in text
    frames = [
        _json.loads(line[len("data: "):])
        for line in text.splitlines()
        if line.startswith("data: ") and line.strip() != "data: [DONE]"
    ]
    calls = [
        call
        for frame in frames
        for choice in frame.get("choices", [])
        for call in (choice.get("delta", {}) or {}).get("tool_calls", [])
    ]
    assert calls
    assert calls[0]["function"]["name"] == "bash"
    assert _json.loads(calls[0]["function"]["arguments"]) == {"command": "ls"}
    assert [
        choice.get("finish_reason")
        for frame in frames
        for choice in frame.get("choices", [])
        if choice.get("finish_reason")
    ] == ["tool_calls"]


@pytest.mark.asyncio
async def test_stream_normalizer_rejects_unparseable_reasoning_tool_markup():
    """An incomplete reasoning envelope must be eligible for pool fallback."""
    import json as _json
    from tusker_gateway.errors import MalformedToolCallError
    from tusker_gateway.endpoints import _normalize_stream

    async def malformed_stream():
        for value in ("<dots_function_call>\n", '<invoke name="bash">\nnot closed'):
            payload = {"choices": [{"delta": {"reasoning": value}}]}
            yield f"data: {_json.dumps(payload)}\n\n".encode()
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    with pytest.raises(MalformedToolCallError):
        async for _ in _normalize_stream(
            malformed_stream(),
            provider="openrouter",
            model="dots-studio/dots-3-note-preview:free",
            tools_requested=True,
        ):
            pass


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


def test_stripper_strips_orphan_closing_tags():
    """Regression: models that emit closing tags (`</parameter>`, `</function>`)
    with no matching opener observed by the stripper must still have those
    tags stripped, otherwise OMP renders them as visible text content.

    Reproduces the OMP-reported bug where the model emitted "thinking aloud"
    prose about how to test an API followed by an orphan `</parameter>\\n</function>`
    that should never have been visible in the first place.
    """
    s = _ToolCallStripper()
    text = (
        "We need to decide next action. Since we haven't yet confirmed "
        "whether NVIDIA API works, we need to continue.\n\n"
        "We'll send POST request without Authorization header:\n\n"
        "curl -i -X POST \"https://integrate.api.nvidia.com/v1/chat/completions\"\n"
        "-H \"Content-Type: application/json\"\n"
        "-d '{\"model\":\"meta/llama-3.1-8b-instruct\"}'\n"
        "</parameter>\n"
        "</function>"
    )
    out = s.feed(text)
    assert "</parameter>" not in out, f"</parameter> leaked: {out!r}"
    assert "</function>" not in out, f"</function> leaked: {out!r}"
    # Surrounding prose is preserved.
    assert "curl -i -X POST" in out
    assert "We need to decide next action" in out


def test_stripper_handles_generic_invoke_block_split_across_chunks():
    s = _ToolCallStripper()
    assert s.feed('<invoke name="bash">') == ""
    assert s.feed('<parameter name="command">ls</parameter>') == ""
    assert s.feed('</invoke> after') == " after"
    blocks = s.drain_pending_blocks()
    assert len(blocks) == 1
    assert blocks[0][1] is True


def test_stripper_handles_generic_opener_split_inside_attribute():
    s = _ToolCallStripper()
    assert s.feed('before <invoke name="') == "before "
    assert s.feed('bash">') == ""
    assert s.feed('<parameter name="command">ls</parameter>') == ""
    assert s.feed('</invoke> after') == " after"
    blocks = s.drain_pending_blocks()
    assert len(blocks) == 1
    assert blocks[0][0].startswith('<invoke name="bash">')


def test_stripper_buffers_json_wrapper_until_closing_tag():
    s = _ToolCallStripper()
    assert s.feed('before <tool_call>{"name":"bash","args":') == "before "
    assert s.feed('{}') == ""
    assert s.feed('}</tool_call> after') == " after"
    blocks = s.drain_pending_blocks()
    assert len(blocks) == 1
    assert '<tool_call>{"name":"bash","args":{}}</tool_call>' in blocks[0][0]


@pytest.mark.asyncio
async def test_stream_normalizer_strips_orphan_closing_tags(client):
    """Streaming integration for orphan closing tags: the streamed delta.content
    must not contain `</parameter>` or `</function>` when the upstream emits
    prose + orphan closer tags without ever producing a `<function=...>` opener.
    """
    async def leaked_stream(*args, **kwargs):
        yield (
            b'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
            b'"content":"We need to decide next action...\\n\\n"}}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"curl -i -X POST https://integrate.api.nvidia.com/v1/chat/completions"}}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"</parameter>\\n</function>"}}]}\n\n'
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
                "messages": [{"role": "user", "content": "test nvidia"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        body = await resp.read()

    text = body.decode("utf-8", errors="replace")
    assert "</parameter>" not in text, f"orphan </parameter> leaked: {text!r}"
    assert "</function>" not in text, f"orphan </function> leaked: {text!r}"
    # Prose around the orphan tags survives intact.
    assert "We need to decide next action" in text
    assert "curl -i -X POST" in text


def test_stripper_drops_empty_args_block_as_malformed():
    """A buffered <function=bash>...</function> block with no
    <parameter=k>v</parameter> siblings (model wrote prose inside the
    block) is recognised as malformed: the inner prose is preserved
    and no fake empty-args tool_call is emitted.
    """
    import asyncio
    import json

    from tusker_gateway.endpoints import _normalize_stream

    async def stream():
        for piece in [
            b'data: {"choices":[{"delta":{"role":"assistant","content":"<function=bash>\\nWe need to decide next action.\\ncurl -X POST ...\\n</parameter>\\n</function>"}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}}]}\n\n',
            b'data: [DONE]\n\n',
        ]:
            yield piece

    async def run():
        seen_text = ""
        seen_tool_calls = []
        async for frame in _normalize_stream(stream()):
            s = frame.strip()
            if not s.startswith(b"data: ") or s == b"data: [DONE]":
                continue
            try:
                obj = json.loads(s[len(b"data: "):])
            except json.JSONDecodeError:
                continue
            for ch in obj.get("choices", []):
                d = ch.get("delta", {}) or {}
                if d.get("content"):
                    seen_text += d["content"]
                if d.get("tool_calls"):
                    seen_tool_calls.extend(d["tool_calls"])

        # Prose inside the malformed block is preserved.
        assert "We need to decide next action" in seen_text
        assert "curl -X POST" in seen_text
        # No fake empty-args tool call was promoted.
        assert seen_tool_calls == [], f"unexpected tool_calls: {seen_tool_calls!r}"

    asyncio.run(run())


@pytest.mark.asyncio
async def test_stream_normalizer_deduplicates_native_call_and_finishes_as_tool_call():
    """A native call plus XML text in one event must be emitted once."""
    import json

    async def stream():
        payload = {
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": (
                        "I will run that. "
                        "<function=bash><parameter=command>ls</parameter></function>"
                    ),
                    "tool_calls": [{
                        "id": "native-1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                    }],
                },
                "finish_reason": "stop",
            }],
        }
        yield f"data: {json.dumps(payload)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    from tusker_gateway.endpoints import _normalize_stream

    frames = []
    async for raw in _normalize_stream(stream(), provider="test", model="m"):
        text = raw.strip()
        if not text.startswith(b"data: ") or text == b"data: [DONE]":
            continue
        frames.append(json.loads(text[len(b"data: "):]))

    tool_deltas = []
    content = []
    finish_reasons = []
    for frame in frames:
        for choice in frame.get("choices", []):
            delta = choice.get("delta", {}) or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("tool_calls"):
                tool_deltas.extend(delta["tool_calls"])
            if choice.get("finish_reason"):
                finish_reasons.append(choice["finish_reason"])

    assert "<function" not in "".join(content)
    assert len(tool_deltas) == 1
    assert tool_deltas[0]["id"] == "native-1"
    assert finish_reasons == ["tool_calls"]


@pytest.mark.asyncio
async def test_stream_normalizer_deduplicates_provider_terminal_frames():
    """Provider duplicate finish/DONE events must not confuse OMP."""
    import json

    async def stream():
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        # OpenRouter can emit a second usage-bearing terminal event and
        # another sentinel after the normal terminal event.
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":1}}\n\n'
        yield b"data: [DONE]\n\n"
        yield b"data: [DONE]\n\n"

    from tusker_gateway.endpoints import _normalize_stream

    frames = []
    async for raw in _normalize_stream(stream(), provider="test", model="m"):
        stripped = raw.strip()
        if not stripped.startswith(b"data: "):
            continue
        if stripped == b"data: [DONE]":
            frames.append("done")
            continue
        frames.append(json.loads(stripped[len(b"data: "):]))

    finish_reasons = [
        choice.get("finish_reason")
        for frame in frames
        if isinstance(frame, dict)
        for choice in frame.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert finish_reasons == ["stop"]
    assert "done" not in frames


@pytest.mark.asyncio
async def test_stream_normalizer_buffers_split_json_tool_wrapper():
    """A JSON wrapper must not emit its payload before the closing tag."""
    import json

    async def stream():
        for content in (
            'before <tool_call>{"name":"bash","args":',
            "{}",
            '}</tool_call> after',
        ):
            payload = {"choices": [{"delta": {"content": content}}]}
            yield f"data: {json.dumps(payload)}\n\n".encode()
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    from tusker_gateway.endpoints import _normalize_stream

    content_parts = []
    tool_calls = []
    async for raw in _normalize_stream(stream(), provider="test", model="m"):
        line = raw.strip()
        if not line.startswith(b"data: ") or line == b"data: [DONE]":
            continue
        obj = json.loads(line[len(b"data: "):])
        for choice in obj.get("choices", []):
            delta = choice.get("delta", {}) or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("tool_calls"):
                tool_calls.extend(delta["tool_calls"])

    assert "<tool_call>" not in "".join(content_parts)
    assert "before" in "".join(content_parts)
    assert "after" in "".join(content_parts)
    assert tool_calls[0]["function"]["name"] == "bash"


@pytest.mark.asyncio
async def test_stream_normalizer_handles_id_suffixed_omp_dots_tool_wrapper():
    """OMP/DOTS wrapper markup is buffered and promoted exactly once."""
    import json

    async def stream():
        for content in (
            "before <tool_calls:abc123><tool_call:abc123>bash<tool_sep:abc123>",
            "<arg_key:abc123>command</arg_key:abc123>"
            "<arg_value:abc123>ls -la</arg_value:abc123>",
            "</tool_call:abc123></tool_calls:abc123> after",
        ):
            payload = {"choices": [{"delta": {"content": content}}]}
            yield f"data: {json.dumps(payload)}\n\n".encode()
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    from tusker_gateway.endpoints import _normalize_stream

    content_parts = []
    tool_calls = []
    finish_reasons = []
    async for raw in _normalize_stream(stream(), provider="test", model="m"):
        line = raw.strip()
        if not line.startswith(b"data: ") or line == b"data: [DONE]":
            continue
        obj = json.loads(line[len(b"data: "):])
        for choice in obj.get("choices", []):
            delta = choice.get("delta", {}) or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("tool_calls"):
                tool_calls.extend(delta["tool_calls"])
            if choice.get("finish_reason"):
                finish_reasons.append(choice["finish_reason"])

    assert "<tool_call" not in "".join(content_parts)
    assert "before" in "".join(content_parts)
    assert "after" in "".join(content_parts)
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "bash"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"command": "ls -la"}
    assert finish_reasons == ["tool_calls"]


@pytest.mark.asyncio
async def test_stream_normalizer_emits_tool_calls_when_opener_split(client):
    """If the <function=bash> opener arrives split across two SSE fragments
    (``<functi`` + ``on=bash>``), the stripper must reassemble it, buffer
    the body, and promote it to a structured delta.tool_calls frame on
    close.
    """
    async def split_opener_stream(*args, **kwargs):
        yield (
            b'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
            b'"content":"I will run that.\\n\\n"},"finish_reason":null}]}\n\n'
        )
        # Opener split across two deltas.
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"<functi"},"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"on=bash>"},"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"\\n<parameter=command>ls</parameter>\\n</function>"},'
            b'"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{},'
            b'"finish_reason":"stop"}]}\n\n'
        )
        yield b'data: [DONE]\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = split_opener_stream()
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "ls please"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        body = await resp.read()

    text = body.decode("utf-8", errors="replace")
    import json as _json
    tool_calls = []
    for line in text.splitlines():
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        try:
            obj = _json.loads(line[len("data: "):])
            for ch in obj.get("choices", []):
                tc = ch.get("delta", {}).get("tool_calls")
                if isinstance(tc, list):
                    tool_calls.extend(tc)
        except Exception:
            continue

    assert tool_calls, f"no tool_calls emitted for split-opener shape: {text!r}"
    fn = tool_calls[0]["function"]
    assert fn["name"] == "bash"
    args = _json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
    assert args.get("command") == "ls"


@pytest.mark.asyncio
async def test_stream_normalizer_emits_tool_calls_when_param_tag_split(client):
    """If a ``<parameter=command>`` tag is split across two SSE fragments
    (``<par`` + ``ameter=command>``), the stripper must reassemble it and
    still promote the block to a structured delta.tool_calls frame with
    the parsed command argument.
    """
    async def split_param_stream(*args, **kwargs):
        yield (
            b'data: {"choices":[{"index":0,"delta":{"role":"assistant",'
            b'"content":"<function=bash>\\n<par"},"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{'
            b'"content":"ameter=command>ls\\n</parameter>\\n</function>"},'
            b'"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"index":0,"delta":{},'
            b'"finish_reason":"stop"}]}\n\n'
        )
        yield b'data: [DONE]\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = split_param_stream()
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "ls"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        assert resp.status == 200
        body = await resp.read()

    text = body.decode("utf-8", errors="replace")
    import json as _json
    tool_calls = []
    for line in text.splitlines():
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        try:
            obj = _json.loads(line[len("data: "):])
            for ch in obj.get("choices", []):
                tc = ch.get("delta", {}).get("tool_calls")
                if isinstance(tc, list):
                    tool_calls.extend(tc)
        except Exception:
            continue

    assert tool_calls, f"no tool_calls emitted for split-param shape: {text!r}"
    fn = tool_calls[0]["function"]
    assert fn["name"] == "bash"
    args = _json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
    assert args.get("command") == "ls"


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
