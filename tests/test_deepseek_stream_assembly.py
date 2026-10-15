"""Regression tests for DeepSeek-style streamed tool-call fragments.

These exercise the gateway's existing SSE assembler without provider credentials.
"""
import json

from tusker_gateway.endpoints import _assemble_stream_tool_calls


def _frame(delta, *, choice=0):
    payload = {"choices": [{"index": choice, "delta": delta}]}
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def test_fragmented_parallel_tool_calls():
    frames = [
        _frame({"reasoning_content": "thinking"}),
        _frame({"tool_calls": [
            {"index": 0, "id": "call_a", "type": "function",
             "function": {"name": "read_", "arguments": '{"path":"'}},
            {"index": 1, "id": "call_b", "type": "function",
             "function": {"name": "list_", "arguments": '{"dir":"'}},
        ]}),
        _frame({"tool_calls": [
            {"index": 0, "function": {"name": "file", "arguments": 'a.txt"}'}},
            {"index": 1, "function": {"name": "files", "arguments": '/tmp"}'}},
        ]}),
    ]
    calls = _assemble_stream_tool_calls(frames)
    assert len(calls) == 2
    assert calls[0]["id"] == "call_a"
    assert calls[0]["function"] == {"name": "read_file", "arguments": '{"path":"a.txt"}'}
    assert calls[1]["function"] == {"name": "list_files", "arguments": '{"dir":"/tmp"}'}


def test_repeated_cumulative_function_name():
    frames = [
        _frame({"tool_calls": [{"index": 0, "id": "call_1",
                 "function": {"name": "read", "arguments": ""}}]}),
        _frame({"tool_calls": [{"index": 0,
                 "function": {"name": "read_file", "arguments": "{}"}}]}),
        _frame({"tool_calls": [{"index": 0,
                 "function": {"name": "read_file", "arguments": ""}}]}),
    ]
    assert _assemble_stream_tool_calls(frames)[0]["function"]["name"] == "read_file"


def test_reasoning_only_chunks_do_not_create_tool_calls():
    assert _assemble_stream_tool_calls([
        _frame({"reasoning_content": "I might call a tool"}),
        _frame({"content": "Answer"}),
    ]) == []


def test_separate_choices_keep_call_indices_distinct():
    frames = [
        _frame({"tool_calls": [{"index": 0, "id": "a",
                 "function": {"name": "alpha", "arguments": "{}"}}]}, choice=0),
        _frame({"tool_calls": [{"index": 0, "id": "b",
                 "function": {"name": "beta", "arguments": "{}"}}]}, choice=1),
    ]
    assert [c["id"] for c in _assemble_stream_tool_calls(frames)] == ["a", "b"]
