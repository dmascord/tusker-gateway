"""Comprehensive tool-format tests for passthrough tool support."""
from __future__ import annotations
import json
import pytest
from tusker_gateway.tool_formats import (
    normalize_tools, normalize_tool_calls, openai_to_anthropic_tools,
    openai_messages_to_anthropic, parse_text_tool_calls, strip_tool_text,
    normalize_response_tool_calls,
)

def test_normalize_tools():
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object", "properties": {"c": {"type": "string"}}}}}]
    res = normalize_tools(tools)
    assert len(res) == 1
    assert res[0]["function"]["name"] == "bash"
    assert normalize_tools([{"name": "x"}])[0]["function"]["name"] == "x"

def test_normalize_tool_calls():
    # OpenAI
    c = normalize_tool_calls([{"id": "c1", "type": "function", "function": {"name": "r", "arguments": "{}"}}])
    assert c[0]["id"] == "c1"
    # Anthropic
    c = normalize_tool_calls([{"type": "tool_use", "id": "u1", "name": "b", "input": {"k": "v"}}])
    assert c[0]["id"] == "u1"
    assert json.loads(c[0]["function"]["arguments"]) == {"k": "v"}
    # Bedrock
    c = normalize_tool_calls([{"toolUse": {"toolUseId": "b1", "name": "w", "input": {"p": "x"}}}])
    assert c[0]["id"] == "b1"

def test_openai_to_anthropic_tools():
    tools = [{"type": "function", "function": {"name": "b", "parameters": {"type": "object", "properties": {}}}}]
    res = openai_to_anthropic_tools(tools)
    assert res[0]["name"] == "b"
    assert "input_schema" in res[0]

def test_openai_messages_to_anthropic():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "tool_calls": [{"id": "c1", "function": {"name": "b", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
    ]
    res = openai_messages_to_anthropic(msgs)
    assert res[0]["role"] == "user" # system -> user
    assert any(b["type"] == "tool_use" for b in res[2]["content"])
    assert res[3]["content"][0]["type"] == "tool_result"

def test_parse_text_tool_calls():
    # XML JSON
    assert parse_text_tool_calls("<tool_call>{\"name\":\"b\",\"args\":{}}</tool_call>")[0]["function"]["name"] == "b"
    # Claude-style
    text = '<function_calls><invoke name="b"><parameter name="c">ls</parameter></invoke></function_calls>'
    assert parse_text_tool_calls(text)[0]["function"]["name"] == "b"
    # DSML
    text = '<ds:function name="s"><ds:parameter name="p">v</ds:parameter></ds:function>'
    assert parse_text_tool_calls(text)[0]["function"]["name"] == "s"
    # Self-closing
    assert parse_text_tool_calls('<tool_invocation name="b" arguments={"c":"ls"} />')[0]["function"]["name"] == "b"
    # TOOL_CALL
    assert parse_text_tool_calls("TOOL_CALL: bash({\"c\":\"ls\"})")[0]["function"]["name"] == "bash"

def test_strip_tool_text():
    text = "Thinking... <tool_call>{}</tool_call>\nAfter tool."
    out = strip_tool_text(text)
    assert "Thinking..." in out
    assert "After tool." in out
    assert "<tool_call>" not in out
    assert strip_tool_text("TOOL_CALL: bash()") == ""

def test_normalize_response_tool_calls():
    resp = {"choices": [{"message": {"role": "assistant", "content": "<tool_call>{\"name\":\"b\"}</tool_call>"}}]}
    res = normalize_response_tool_calls(resp)
    msg = res["choices"][0]["message"]
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "b"
    assert msg["content"] == "" # stripped
