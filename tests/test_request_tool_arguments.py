"""Preflight rejection of malformed tool-call arguments in request history.

A client-supplied tool call with non-JSON ``arguments`` makes every upstream
candidate reject the whole turn. The gateway must fail at the public boundary
with an actionable message instead of exhausting failover (2026-11-25:
req_9bddd80866c784c1, req_7e1be45c37b89344).
"""
from __future__ import annotations

import pytest

from tusker_gateway.endpoints import _validate_chat_body
from tusker_gateway.errors import BadRequestError

from .conftest import HEADERS_AUTH


def _call(arguments, *, name="read"):
    return {
        "id": "chatcmpl-tool-test",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _body(tool_calls, *, stream=False):
    return {
        "model": "hermes-code",
        "messages": [
            {"role": "user", "content": "inspect"},
            {"role": "assistant", "content": None, "tool_calls": tool_calls},
        ],
        "stream": stream,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        '{"path": "a\\q"}',  # invalid JSON escape, as seen in the incident
        '{"path": "unterminated}',
        "[1, 2]",  # valid JSON, but not an object
        '"just a string"',
        "42",
    ],
)
def test_rejects_malformed_tool_call_arguments(arguments):
    with pytest.raises(BadRequestError) as exc:
        _validate_chat_body(_body([_call(arguments)]))
    assert exc.value.code == "invalid_tool_call_arguments"
    assert "messages[1].tool_calls[0].function.arguments" in str(exc.value)
    assert "tool 'read'" in str(exc.value)


def test_rejects_malformed_legacy_function_call_arguments():
    body = {
        "model": "hermes-code",
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "function_call": {"name": "read", "arguments": "{oops"},
            }
        ],
    }
    with pytest.raises(BadRequestError) as exc:
        _validate_chat_body(body)
    assert "messages[0].function_call[0]" in str(exc.value)


@pytest.mark.parametrize(
    "arguments",
    [
        "{}",
        '{"path": "/tmp/a", "n": 3}',
        "",  # no-argument tool call, common in the wild
        "   ",
        {"path": "/tmp/a"},  # already-parsed object from a permissive client
    ],
)
def test_accepts_valid_or_no_argument_tool_calls(arguments):
    body = _body([_call(arguments)])
    assert _validate_chat_body(body) is body


def test_does_not_inspect_provider_output_validation_path():
    """Only client request history is validated; model output stays with OMP."""
    from tusker_gateway.endpoints import _validate_tool_call_arguments

    # A partial argument fragment from a provider is deliberately tolerated.
    _validate_tool_call_arguments(
        [{"function": {"name": "read", "arguments": '{"path": "'}}],
        tools=[],
        provider="kilo",
        model="test",
        request_id="req_test",
    )


@pytest.mark.asyncio
async def test_chat_returns_400_before_dispatch(client):
    payload = _body([_call('{"path": "a\\q"}')], stream=True)
    resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
    assert resp.status == 400
    data = await resp.json()
    assert data["error"]["code"] == "invalid_tool_call_arguments"
    assert "messages[1].tool_calls[0]" in data["error"]["message"]
