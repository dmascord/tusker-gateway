"""Native OMP question-tool approval scenarios."""
from __future__ import annotations

import json

import pytest

from tusker_gateway.endpoints import _validate_complete_tool_response
from tusker_gateway.endpoints import _prepare_stream_result
from tusker_gateway.sse import sse_frame
from tusker_gateway.native_question import question_authorized, question_response_for_calls, reset_pending


@pytest.fixture(autouse=True)
def clear_pending_questions():
    reset_pending()
    yield
    reset_pending()


def _trade_call(qty=1):
    return [{
        "id": "call-trade",
        "type": "function",
        "function": {"name": "place_trade", "arguments": json.dumps({"qty": qty})},
    }]


def test_risky_call_becomes_native_question_tool_call():
    response = question_response_for_calls(_trade_call(), model="model")
    assert response is not None
    call = response["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "ask"
    args = json.loads(call["function"]["arguments"])
    assert args["questions"][0]["header"] == "Approval"
    assert {option["label"] for option in args["questions"][0]["options"]} == {
        "Allow once", "Deny"
    }


def test_question_result_authorizes_exact_call():
    question = question_response_for_calls(_trade_call(), model="model")
    call = question["choices"][0]["message"]["tool_calls"][0]
    messages = [
        question["choices"][0]["message"],
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": json.dumps({
                "results": [{"id": "?", "selectedOptions": ["Allow once"]}],
            }),
        },
    ]
    assert question_authorized(messages, _trade_call()) is True


def test_question_deny_and_unrecognized_answer_do_not_authorize():
    question = question_response_for_calls(_trade_call(), model="model")
    call = question["choices"][0]["message"]["tool_calls"][0]
    messages = [
        question["choices"][0]["message"],
        {"role": "tool", "tool_call_id": call["id"], "content": "Deny"},
    ]
    assert question_authorized(messages, _trade_call()) is False


def test_question_result_cannot_authorize_changed_arguments():
    question = question_response_for_calls(_trade_call(qty=1), model="model")
    call = question["choices"][0]["message"]["tool_calls"][0]
    messages = [
        question["choices"][0]["message"],
        {"role": "tool", "tool_call_id": call["id"], "content": "Allow once"},
    ]
    assert question_authorized(messages, _trade_call(qty=2)) is False


def test_complete_guard_emits_question_then_accepts_original_call():
    response = {
        "choices": [{"message": {"role": "assistant", "tool_calls": _trade_call()}}]
    }
    tools = [{"type": "function", "function": {"name": "place_trade"}}]
    question = _validate_complete_tool_response(
        response,
        tools,
        provider="provider",
        model="model",
        request_id="req-1",
        require_tool_call=False,
        reject_empty=False,
        native_questions=True,
    )
    question_call = question["choices"][0]["message"]["tool_calls"][0]
    messages = [
        question["choices"][0]["message"],
        {"role": "tool", "tool_call_id": question_call["id"], "content": "Allow once"},
    ]
    allowed = _validate_complete_tool_response(
        response,
        tools,
        provider="provider",
        model="model",
        request_id="req-2",
        require_tool_call=False,
        reject_empty=False,
        messages=messages,
        native_questions=True,
    )
    assert allowed == response


@pytest.mark.asyncio
async def test_streaming_risky_call_is_replaced_with_question_tool():
    async def upstream():
        payload = {
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "tool_calls": _trade_call()},
                "finish_reason": "tool_calls",
            }],
        }
        yield sse_frame(payload)
        yield b"data: [DONE]\n\n"

    prepared = await _prepare_stream_result(
        upstream(),
        provider="provider",
        model="model",
        request_id="req-stream",
        tools_requested=True,
        tools=[{"type": "function", "function": {"name": "place_trade"}}],
        messages=[{"role": "user", "content": "please do it"}],
    )
    frames = [frame async for frame in prepared]
    joined = b"".join(frames)
    assert b'"name": "ask"' in joined


@pytest.mark.asyncio
async def test_streaming_destructive_bash_call_is_replaced_before_client_execution():
    async def provider_stream():
        payload = {
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-bash",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command":"rm -rf /tmp/omp-proof"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }
        yield sse_frame(payload)
        yield b"data: [DONE]\n\n"
    result = await _prepare_stream_result(
        provider_stream(),
        tools=[{"type": "function", "function": {"name": "bash"}}],
        tool_choice=None,
        require_tool_call=False,
        tools_requested=True,
        provider="test",
        model="model",
        request_id="req-bash",
        explicitly_authorized=False,
        greylisted=False,
        argument_regex=None,
        content_regex=None,
        messages=[],
        force_deny=False,
    )
    joined = b"".join([frame async for frame in result])
    assert b'"name": "ask"' in joined
    assert b'"name": "place_trade"' not in joined
    assert b'"name": "bash"' not in joined
