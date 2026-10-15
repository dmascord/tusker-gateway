"""No-payload telemetry tests."""
from tusker_gateway.tool_telemetry import summarize_chat_tool_result


def test_valid_tool_calls():
    result = {"choices": [{"finish_reason": "tool_calls", "message": {
        "reasoning_content": "private",
        "tool_calls": [{"id": "a", "type": "function",
                        "function": {"name": "secret_name", "arguments": '{"password":"secret"}'}}],
    }}]}
    summary = summarize_chat_tool_result(result, requested=True)
    assert summary["returned"] == 1
    assert summary["invalid_arguments"] == 0
    assert summary["reasoning_present"] is True
    assert "secret" not in repr(summary)
    assert "password" not in repr(summary)


def test_malformed_and_duplicate_calls():
    result = {"choices": [{"message": {"tool_calls": [
        {"id": "a", "function": {"arguments": "{broken"}},
        {"id": "a", "function": {"arguments": "[]"}},
        {"function": {"arguments": "{}"}},
    ]}}]}
    summary = summarize_chat_tool_result(result, requested=True)
    assert summary["returned"] == 3
    assert summary["invalid_arguments"] == 2
    assert summary["duplicate_ids"] == 1
    assert summary["missing_ids"] == 1


def test_no_tools():
    assert summarize_chat_tool_result({"choices": [{"message": {"content": "hi"}}]}, requested=False)["returned"] == 0
