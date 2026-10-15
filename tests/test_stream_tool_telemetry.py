"""Streaming tool telemetry regression tests; no provider credentials required."""
import json

from tusker_gateway.stream_tool_telemetry import StreamToolTelemetry


def frame(delta=None, *, finish=None):
    choice = {"index": 0, "delta": delta or {}, "finish_reason": finish}
    return ("data: " + json.dumps({"choices": [choice]}) + "\n\n").encode()


def test_fragmented_calls_and_reasoning():
    health = StreamToolTelemetry(requested=True)
    health.feed(frame({"reasoning_content": "private thoughts"}))
    health.feed(frame({"tool_calls": [{"index": 0, "id": "abc", "function": {
        "name": "secret", "arguments": '{"x":'}}]}))
    health.feed(frame({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}))
    health.feed(frame(finish="tool_calls"))
    health.feed(b"data: [DONE]\n\n")
    summary = health.summary()
    assert summary["returned"] == 1
    assert summary["invalid_arguments"] == 0
    assert summary["incomplete_stream"] is False
    assert summary["reasoning_present"] is True
    assert "private thoughts" not in repr(summary)
    assert "secret" not in repr(summary)


def test_conflicting_id_and_incomplete():
    health = StreamToolTelemetry(requested=True)
    health.feed(frame({"tool_calls": [{"index": 0, "id": "a", "function": {"arguments": "{"}}]}))
    health.feed(frame({"tool_calls": [{"index": 0, "id": "b", "function": {"arguments": "}"}}]}))
    summary = health.summary()
    assert summary["conflicting_ids"] == 1
    assert summary["incomplete_stream"] is True


def test_finish_reason_mismatch():
    health = StreamToolTelemetry(requested=True)
    health.feed(frame({"tool_calls": [{"index": 0, "id": "a", "function": {"arguments": "{}"}}]}))
    health.feed(frame(finish="stop"))
    health.feed(b"data: [DONE]\n\n")
    assert health.summary()["finish_mismatch"] is True


def test_failover_drops_previous_candidate():
    health = StreamToolTelemetry(requested=True)
    health.feed(frame({"tool_calls": [{"index": 0, "id": "first", "function": {"arguments": "{"}}]}))
    health.reset_for_fallback()
    health.feed(frame({"tool_calls": [{"index": 0, "id": "second", "function": {"arguments": "{}"}}]}))
    health.feed(frame(finish="tool_calls"))
    health.feed(b"data: [DONE]\n\n")
    assert health.summary()["returned"] == 1
    assert health.summary()["invalid_arguments"] == 0
