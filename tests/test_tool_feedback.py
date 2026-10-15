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
