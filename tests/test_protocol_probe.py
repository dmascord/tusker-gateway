"""Protocol probe classifiers: no network requests are made here."""
from tusker_gateway.protocol_probe import classify_probe


def _response(arguments='{"query":"hello"}', finish="tool_calls"):
    return {"choices": [{"finish_reason": finish, "message": {
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "search", "arguments": arguments}}],
    }}]}


def test_structured_tool_probe_requires_valid_json():
    assert classify_probe("structured_tool_calls", response=_response()).status == "passed"
    assert classify_probe("structured_tool_calls", response=_response("{")).status == "unknown"


def test_tool_finish_probe_does_not_assume_stop_is_compliant():
    assert classify_probe("tool_call_finish", response=_response()).status == "passed"
    assert classify_probe("tool_call_finish", response=_response(finish="stop")).status == "unknown"


def test_reasoning_probe_requires_explicit_channel():
    assert classify_probe("reasoning_channel", response=_response()).status == "unknown"
    response = _response()
    response["choices"][0]["message"]["reasoning_content"] = "thinking"
    assert classify_probe("reasoning_channel", response=response).status == "passed"


def test_rate_limit_and_transport_are_not_unsupported():
    assert classify_probe("structured_tool_calls", http_status=429).status == "unavailable"
    assert classify_probe("structured_tool_calls", transport_error=True).status == "unavailable"


def test_replay_requires_multi_turn_probe():
    assert classify_probe("reasoning_replay", response=_response()).status == "unknown"


def test_failover_does_not_mutate_original_history():
    from tusker_gateway.protocol_compat import adapt_upstream_chat
    messages = [{"role": "assistant", "reasoning": "original", "content": None}]
    native = {"messages": messages}
    adapt_upstream_chat(native, "deepseek", "deepseek-chat")
    assert native["messages"] is not messages
    assert native["messages"][0]["reasoning_content"] == "original"
    assert "reasoning_content" not in messages[0]
    routed = {"messages": messages}
    adapt_upstream_chat(routed, "openrouter", "deepseek-chat")
    assert routed["messages"] is messages
