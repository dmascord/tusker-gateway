"""Privacy-preserving tool-shape learning tests."""
from __future__ import annotations

import json

import pytest

from tusker_gateway.tool_learning import observe_tool_calls, reset, snapshot


@pytest.fixture(autouse=True)
def clear_catalogue():
    reset()
    yield
    reset()


TOOLS = [{
    "type": "function",
    "function": {
        "name": "grep",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern", "path"],
        },
    },
}]


class _Audit:
    def __init__(self):
        self.events = []

    def write_sync(self, event):
        self.events.append(dict(event))


def _call(arguments):
    return [{
        "id": "call-grep",
        "type": "function",
        "function": {"name": "grep", "arguments": json.dumps(arguments)},
    }]


def test_observation_records_shape_without_argument_values():
    audit = _Audit()
    observe_tool_calls(
        _call({"path": "/private/project", "regex": "secret-value"}),
        TOOLS,
        provider="provider",
        model="model",
        request_id="req-shape-1",
        audit=audit,
    )

    event = audit.events[0]
    assert event["event_type"] == "tool.schema.observation"
    assert event["outcome"] == "incomplete"
    assert event["missing_required"] == ["pattern"]
    assert event["unexpected_keys"] == ["regex"]
    assert "secret-value" not in json.dumps(event)
    assert "/private/project" not in json.dumps(event)


def test_correction_candidate_is_catalogued_from_follow_up_shape():
    audit = _Audit()
    previous = [{
        "role": "assistant",
        "tool_calls": [{
            "id": "call-old",
            "type": "function",
            "function": {
                "name": "grep",
                "arguments": json.dumps({"regex": "old", "path": "/tmp"}),
            },
        }],
    }]
    observe_tool_calls(
        _call({"pattern": "new", "path": "/tmp"}),
        TOOLS,
        messages=previous,
        request_id="req-shape-2",
        audit=audit,
    )

    candidates = snapshot()["candidate_aliases"]
    assert candidates == [{
        "tool_name": "grep",
        "schema_fingerprint": candidates[0]["schema_fingerprint"],
        "source_key": "regex",
        "target_key": "pattern",
        "observed": 1,
        "successful_correction": 1,
    }]
    assert any(event["event_type"] == "tool.schema.correction_candidate" for event in audit.events)


def test_invalid_arguments_are_catalogued_without_raw_payload():
    audit = _Audit()
    observe_tool_calls(
        [{
            "function": {"name": "grep", "arguments": "{not-json secret-value"},
        }],
        TOOLS,
        audit=audit,
    )

    result = snapshot()
    assert result["observations"][0]["shape"] == "invalid_json"
    assert result["observations"][0]["invalid"] == 1
    assert "secret-value" not in json.dumps(result)
