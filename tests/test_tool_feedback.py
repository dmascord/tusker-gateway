"""Standard OpenAI tool-feedback correlation tests."""
from tusker_gateway.tool_feedback import (
    register_generated_calls, correlate_chat_messages, chat_result_call_ids,
)


def test_chat_feedback_matches_once_without_payloads():
    register_generated_calls(["test_call_feedback_123"])
    msg = [{"role": "tool", "tool_call_id": "test_call_feedback_123", "content": "SECRET"}]
    first = correlate_chat_messages(msg)
    assert first["matched_results"] == 1
    assert first["untracked_results"] == 0
    assert "SECRET" not in repr(first)
    second = correlate_chat_messages(msg)
    assert second["matched_results"] == 0
    assert second["untracked_results"] == 1


def test_duplicate_feedback_is_counted():
    register_generated_calls(["test_call_feedback_dup"])
    msg = [{"role": "tool", "tool_call_id": "test_call_feedback_dup"}] * 2
    result = correlate_chat_messages(msg)
    assert result["matched_results"] == 1
    assert result["duplicate_results"] == 1


def test_call_ids_from_normalized_chat():
    result = {"choices": [{"message": {"tool_calls": [{"id": "call_xyz"}]}}]}
    assert chat_result_call_ids(result) == ["call_xyz"]


def test_historical_feedback_is_ignored():
    register_generated_calls(["history_call_123"])
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "history_call_123"}]},
        {"role": "tool", "tool_call_id": "history_call_123", "content": "done"},
        {"role": "assistant", "content": "continue"},
    ]
    result = correlate_chat_messages(messages)
    assert result["historical_results"] == 1
    assert result["untracked_results"] == 0


def test_explicit_error_feedback():
    register_generated_calls(["error_call_123"])
    result = correlate_chat_messages([
        {"role": "tool", "tool_call_id": "error_call_123",
         "content": {"status": "failed"}}
    ])
    assert result["explicit_errors"] == 1


def test_responses_feedback():
    from tusker_gateway.tool_feedback import correlate_responses_input, response_result_call_ids
    assert response_result_call_ids([{"type": "function_call", "call_id": "resp_call_123"}]) == ["resp_call_123"]
    register_generated_calls(["resp_call_123"])
    result = correlate_responses_input([
        {"type": "function_call_output", "call_id": "resp_call_123", "output": "done"}
    ])
    assert result["matched_results"] == 1
