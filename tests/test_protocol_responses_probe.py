"""Responses API function-call classifier fixtures."""
from tusker_gateway.protocol_responses_probe import classify_responses_function_call


def test_responses_function_call_contract():
    result = classify_responses_function_call({
        "status": "completed", "output": [{
            "type": "function_call", "name": "protocol_probe_echo",
            "call_id": "call_1", "arguments": '{"value":"ok"}',
        }],
    })
    assert result.status == "passed"


def test_responses_message_is_not_a_function_call():
    assert classify_responses_function_call({
        "status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": "ok"}],
        }],
    }).status == "unknown"


def test_responses_rejects_wrong_arguments():
    assert classify_responses_function_call({
        "status": "completed", "output": [{
            "type": "function_call", "name": "protocol_probe_echo",
            "call_id": "call_1", "arguments": '{"value":"wrong"}',
        }],
    }).status == "unknown"
