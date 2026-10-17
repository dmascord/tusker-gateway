"""Regression test: golden capture fixtures must contain a real client's
tool-result continuation shape (assistant tool_calls + tool role).

These are the wire shapes the runbook's Phase 2.2 requires: the fixtures must
not be hand-written sketches, they must be real client requests reduced to
their protocol structure. The validator in
``capture_golden_sessions --check`` enforces the privacy side; this test
enforces the coverage side.
"""
from __future__ import annotations

import json

import pytest

from tusker_gateway.tools.capture_golden_sessions import (
    FIXTURE_DIR,
    _is_continuation,
    check_fixtures,
)

# Clients captured on this workstation. Cline is absent here, so it is
# documented as NOT TESTED rather than asserted.
CAPTURED_CLIENTS = ["opencode", "openai-sdk"]


def _fixture(client: str) -> dict:
    path = FIXTURE_DIR / f"{client}.json"
    assert path.exists(), f"missing golden fixture for {client}: {path}"
    return json.loads(path.read_text())


@pytest.mark.parametrize("client", CAPTURED_CLIENTS)
def test_captured_fixture_has_continuation_shape(client: str):
    record = _fixture(client)
    requests = record["requests"]
    assert len(requests) >= 2, (
        f"golden fixture for {client} should capture both the initial "
        f"request and a continuation; got only "
        + repr([r["message_roles"] for r in requests])
    )
    # The final request must be a real tool-result continuation: the client
    # ran the tool and is sending the result back so the model can finish.
    final = requests[-1]
    assert _is_continuation(final), (
        f"{client}: final request must carry assistant tool_calls AND a tool "
        f"role; roles={final['message_roles']!r}"
    )
    # Protocol structure preserved: the tool schema, the assistant tool call
    # and the call id linking the result back are what a client assembles on.
    assert final["tools"], "continuation should carry the tool schemas"
    assistant_messages = [
        m for m in final["messages"] if m.get("role") == "assistant"
    ]
    assert any(m.get("tool_calls") for m in assistant_messages), (
        "assistant.tool_calls missing on continuation"
    )
    tool_message = next(
        m for m in final["messages"] if m.get("role") == "tool"
    )
    assert tool_message.get("tool_call_id"), (
        "tool result must carry a tool_call_id back to the model"
    )


def test_fixtures_contain_no_private_content():
    """The fixture validator must succeed on the committed fixture set."""
    assert check_fixtures() == 0


def test_capture_tool_loads():
    """Smoke: the tool module imports and exposes the public surface."""
    from tusker_gateway.tools import capture_golden_sessions as mod

    assert callable(mod.sanitize_request)
    assert callable(mod.sanitize_message)
    assert callable(mod.sanitize_tools)
    assert callable(mod.sanitize_headers)
