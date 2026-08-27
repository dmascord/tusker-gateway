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


def test_normalize_response_tool_calls_strips_duplicate_native_markup():
    """Native calls must not leave a second executable-looking text copy."""
    resp = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": (
                    "I will run that.\n"
                    "<function=bash>"
                    "<parameter=command>ls</parameter>"
                    "</function>"
                ),
                "tool_calls": [{
                    "id": "native-1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }

    message = normalize_response_tool_calls(resp)["choices"][0]["message"]
    assert message["content"] == "I will run that."
    assert len(message["tool_calls"]) == 1
    assert message["tool_calls"][0]["id"] == "native-1"


def test_strip_tool_text_removes_json_and_namespaced_invocation_envelopes():
    text = (
        "before <tool_call>{\"name\":\"bash\",\"args\":{}}"
        "</tool_call>"
        "<dsml:invoke name=\"bash\"><dsml:parameter name=\"x\">1"
        "</dsml:parameter></dsml:invoke> after"
    )
    assert strip_tool_text(text) == "before  after"


def test_omp_dots_id_suffixed_tool_envelope_is_parsed_and_removed():
    text = (
        "before "
        "<tool_calls:abc123><tool_call:abc123>bash<tool_sep:abc123>"
        "<arg_key:abc123>command</arg_key:abc123>"
        "<arg_value:abc123>ls -la</arg_value:abc123>"
        "</tool_call:abc123></tool_calls:abc123>"
        " after"
    )
    calls = parse_text_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "bash"
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls -la"}
    assert strip_tool_text(text) == "before  after"

def test_malformed_hermes_xml_function_block():
    """<function=name> with bare name and sibling <parameter=k>v</parameter> tags.

    This is the malformed Hermes/Qwen pattern observed in production where a
    provider emits tool-call XML inside <tool_call>...</tool_call> but uses
    <function=name> instead of <invoke name="name"> and puts parameters as
    siblings rather than children.
    """
    text = (
        '<tool_call>\n'
        '<function=bash>\n'
        '<parameter=command>ssh wildduck \'docker exec ...\'</parameter>\n'
        '<parameter=timeout>15</parameter>\n'
        '</function>\n'
        '</tool_call>'
    )
    calls = parse_text_tool_calls(text)
    assert isinstance(calls, list), "parse_text_tool_calls must return a list, not None"
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "bash"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args == {"command": "ssh wildduck 'docker exec ...'", "timeout": "15"}
    # Stripping the markup must leave ordinary assistant text intact.
    assert strip_tool_text("Before\n" + text + "\nAfter") == "Before\n\nAfter"

def test_malformed_hermes_xml_with_leading_text():
    text = (
        'Sure, let me run that.\n\n'
        '<tool_call>\n'
        '<function=bash>\n'
        '<parameter=command>ls -la</parameter>\n'
        '</function>\n'
        '</tool_call>'
    )
    calls = parse_text_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "bash"
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls -la"}

def test_parse_text_tool_calls_returns_list_never_none():
    """Regression: parse_text_tool_calls must always return a list."""
    # Previously the function had no `return` statement and returned None for
    # any text that didn't trigger an earlier pattern branch, breaking the
    # downstream `bool(calls)` check in normalize_response_tool_calls.
    for text in ["", "plain text", "<tool_call></tool_call>", "no tool call here"]:
        result = parse_text_tool_calls(text)
        assert isinstance(result, list), f"got {type(result).__name__} for {text!r}"
