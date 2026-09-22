"""Native OMP question-tool approval scenarios."""
from __future__ import annotations

import json
import re
import uuid

import pytest

from tusker_gateway.endpoints import _native_content_question_if_needed
from tusker_gateway.endpoints import _validate_complete_tool_response
from tusker_gateway.endpoints import _prepare_stream_result
from tusker_gateway.question_adapters import adapter_for_name
from tusker_gateway.sse import sse_frame
from tusker_gateway.native_question import (
    _content_approval_preview,
    question_authorized,
    question_authorized_for_content,
    question_response_for_calls,
    question_response_for_content,
    replay_approved_tool_response,
    reset_pending,
)


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


class _Audit:
    def __init__(self):
        self.events = []

    def write_sync(self, event):
        self.events.append(dict(event))


def test_risky_call_becomes_native_question_tool_call():
    response = question_response_for_calls(_trade_call(), model="model")
    assert response is not None
    call = response["choices"][0]["message"]["tool_calls"][0]
    assert str(uuid.UUID(call["id"])) == call["id"]
    assert call["function"]["name"] == "ask"
    args = json.loads(call["function"]["arguments"])
    assert args["questions"][0]["header"] == "Approval"
    assert args["questions"][0]["id"] == call["id"]
    assert {option["label"] for option in args["questions"][0]["options"]} == {
        "Allow once", "Deny"
    }


def test_risky_call_question_shows_the_proposed_arguments_before_execution():
    call = [{
        "id": "call-bash",
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": json.dumps({
                "command": "cd /srv/app && rsync -a ./build/ visor:/srv/app/ && rm -rf /tmp/build",
            }),
        },
    }]
    response = question_response_for_calls(call, model="model")
    args = json.loads(
        response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    )
    question = args["questions"][0]["question"]
    assert "bash command:" in question
    assert "cd /srv/app && rsync -a ./build/ visor:/srv/app/ && rm -rf /tmp/build" in question
    assert "Review these arguments before approving." in question


def test_risky_call_question_redacts_credentials_and_bounds_preview():
    secret = "sk-live-1234567890abcdef"
    call = [{
        "id": "call-bash",
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": json.dumps({
                "command": f"rm -rf /tmp/build && curl -H 'Authorization: Bearer {secret}' " + "x" * 2000,
                "api_key": "another-secret-value",
            }),
        },
    }]
    response = question_response_for_calls(call, model="model")
    args = json.loads(
        response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    )
    question = args["questions"][0]["question"]
    assert secret not in question
    assert "another-secret-value" not in question
    assert "[redacted]" in question
    assert len(question) < 800


def test_question_result_authorizes_exact_call():
    question = question_response_for_calls(_trade_call(), model="model")
    call = question["choices"][0]["message"]["tool_calls"][0]
    args = json.loads(call["function"]["arguments"])
    assert args["questions"][0]["id"] == call["id"]
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


def test_content_approval_preview_preserves_line_breaks():
    preview = _content_approval_preview([
        {"role": "user", "content": "submit_order\n\nprice: 123\nquantity: 4"},
    ])
    assert preview == "submit_order\n\nprice: 123\nquantity: 4"


def test_pending_content_question_re_renders_for_a_different_harness():
    messages = [{"role": "user", "content": "please submit_order"}]
    first = question_response_for_content(
        messages,
        "user_content",
        model="model",
        adapter=adapter_for_name("opencode"),
    )
    second = question_response_for_content(
        messages,
        "user_content",
        model="model",
        adapter=adapter_for_name("cline"),
    )
    first_call = first["choices"][0]["message"]["tool_calls"][0]
    second_call = second["choices"][0]["message"]["tool_calls"][0]
    assert first_call["id"] == second_call["id"]
    assert first_call["function"]["name"] == "question"
    assert second_call["function"]["name"] == "ask_question"
    assert json.loads(second_call["function"]["arguments"])["question_id"] == second_call["id"]


def test_question_user_answer_authorizes_exact_call():
    """OMP clients may return the selected option as a user turn."""
    question = question_response_for_calls(_trade_call(), model="model")
    question_message = question["choices"][0]["message"]
    assert question_authorized(
        [
            question_message,
            {"role": "user", "content": "Allow once"},
        ],
        _trade_call(),
    ) is True


def test_question_user_deny_answer_does_not_authorize_exact_call():
    question = question_response_for_calls(_trade_call(), model="model")
    question_message = question["choices"][0]["message"]
    assert question_authorized(
        [
            question_message,
            {"role": "user", "content": "Deny"},
        ],
        _trade_call(),
    ) is False


def test_approved_call_is_replayed_without_model_round_trip():
    original = _trade_call(qty=7)
    question = question_response_for_calls(original, model="model")
    ask_message = question["choices"][0]["message"]
    replay = replay_approved_tool_response([
        ask_message,
        {"role": "user", "content": "Allow once"},
    ])

    assert replay is not None
    assert replay["choices"][0]["finish_reason"] == "tool_calls"
    assert replay["choices"][0]["message"]["tool_calls"] == original
    # One-time approval: a duplicate answer cannot replay the call again.
    assert replay_approved_tool_response([
        ask_message,
        {"role": "user", "content": "Allow once"},
    ]) is None


def test_namespaced_omp_question_id_is_replayed_without_provider_round_trip():
    original = _trade_call(qty=9)
    question = question_response_for_calls(original, model="model")
    ask_message = json.loads(json.dumps(question["choices"][0]["message"]))
    ask_message["tool_calls"][0]["id"] = "default_api:ask"
    ask_message["tool_calls"][0]["function"]["name"] = "default_api:ask"
    replay = replay_approved_tool_response([
        ask_message,
        {
            "role": "tool",
            "tool_call_id": "default_api:ask",
            "content": json.dumps({"selectedOptions": ["Allow once"]}),
        },
    ])
    assert replay is not None
    assert replay["choices"][0]["message"]["tool_calls"] == original


def test_question_deny_and_unrecognized_answer_do_not_authorize():
    question = question_response_for_calls(_trade_call(), model="model")
    call = question["choices"][0]["message"]["tool_calls"][0]
    messages = [
        question["choices"][0]["message"],
        {"role": "tool", "tool_call_id": call["id"], "content": "Deny"},
    ]
    assert question_authorized(messages, _trade_call()) is False


def test_content_request_becomes_native_question_and_accepts_exact_text():
    original_messages = [{"role": "user", "content": "Please delete the old file."}]
    question = question_response_for_content(
        original_messages,
        "user_content",
        model="model",
    )
    call = question["choices"][0]["message"]["tool_calls"][0]
    messages = [
        *original_messages,
        question["choices"][0]["message"],
        {"role": "tool", "tool_call_id": call["id"], "content": "Allow once"},
    ]

    assert question_authorized_for_content(messages, "user_content") is True


def test_content_question_explains_request_without_copying_secret():
    question = question_response_for_content(
        [{
            "role": "user",
            "content": "Please execute the order using Bearer super-secret-token-12345.",
        }],
        "user_content",
        model="model",
    )
    call = question["choices"][0]["message"]["tool_calls"][0]
    args = json.loads(call["function"]["arguments"])
    text = args["questions"][0]["question"]
    assert "execute the order" in text
    assert "super-secret-token-12345" not in text
    assert "Allow the model to continue this request?" in text


def test_content_question_shows_the_detected_phrase():
    question = question_response_for_content(
        [{"role": "user", "content": "Continue the work."}],
        "user_content",
        model="model",
        matched_text="submit the order",
    )
    call = question["choices"][0]["message"]["tool_calls"][0]
    args = json.loads(call["function"]["arguments"])
    assert "submit the order" in args["questions"][0]["question"]


def test_content_question_preflight_is_single_and_avoids_provider_identity():
    messages = [{"role": "user", "content": "Please submit_order now."}]
    regex = re.compile(r"submit[_ -]?order", re.IGNORECASE)

    first = _native_content_question_if_needed(
        messages,
        model="requested-model",
        content_regex=regex,
        request_id="req-preflight",
    )
    assert first is not None
    call = first["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "ask"

    # The approval is carried into the next client turn; once accepted, the
    # preflight no longer emits another question before provider dispatch.
    follow_up = [
        *messages,
        first["choices"][0]["message"],
        {"role": "tool", "tool_call_id": call["id"], "content": "Allow once"},
    ]
    assert _native_content_question_if_needed(
        follow_up,
        model="requested-model",
        content_regex=regex,
        request_id="req-approved",
    ) is None


def test_repeated_content_preflight_reuses_same_question_id():
    messages = [{"role": "user", "content": "Please submit_order now."}]
    regex = re.compile(r"submit[_ -]?order", re.IGNORECASE)
    first = _native_content_question_if_needed(
        messages,
        model="requested-model",
        content_regex=regex,
        request_id="req-first",
    )
    retry = _native_content_question_if_needed(
        messages,
        model="requested-model",
        content_regex=regex,
        request_id="req-retry",
    )
    assert first is retry
    first_call = first["choices"][0]["message"]["tool_calls"][0]
    retry_call = retry["choices"][0]["message"]["tool_calls"][0]
    assert first_call["id"] == retry_call["id"]


def test_replayed_transcript_history_does_not_create_new_content_question():
    regex = re.compile(r"submit[_ -]?order", re.IGNORECASE)
    first = _native_content_question_if_needed(
        [{"role": "user", "content": "Please submit_order now."}],
        model="requested-model",
        content_regex=regex,
        request_id="req-first",
    )
    replay = [
        {"role": "user", "content": "Earlier unrelated context."},
        {"role": "assistant", "content": "Previous answer."},
        {"role": "user", "content": "Please submit_order now."},
    ]
    retry = _native_content_question_if_needed(
        replay,
        model="requested-model",
        content_regex=regex,
        request_id="req-retry",
    )
    assert retry is first


def test_content_question_accepts_result_with_embedded_question_id():
    original_messages = [{"role": "user", "content": "Please execute the order."}]
    question = question_response_for_content(original_messages, "user_content", model="model")
    call = question["choices"][0]["message"]["tool_calls"][0]
    args = json.loads(call["function"]["arguments"])
    result_messages = [
        *original_messages,
        {
            "role": "tool",
            "content": json.dumps({
                "id": args["questions"][0]["id"],
                "selectedOptions": ["Allow once"],
            }),
        },
    ]
    assert question_authorized_for_content(result_messages, "user_content") is True


def test_content_question_accepts_unbound_omp_tool_result():
    """OMP may return the selection without preserving tool_call_id."""
    original_messages = [{"role": "user", "content": "Please submit_order now."}]
    question = question_response_for_content(original_messages, "user_content", model="model")
    result_messages = [
        *original_messages,
        question["choices"][0]["message"],
        {"role": "tool", "content": "Allow once"},
    ]
    assert question_authorized_for_content(result_messages, "user_content") is True


def test_content_question_accepts_selected_options_after_duplicate_result():
    original_messages = [{"role": "user", "content": "Please submit_order now."}]
    question = question_response_for_content(original_messages, "user_content", model="model")
    call = question["choices"][0]["message"]["tool_calls"][0]
    result_messages = [
        *original_messages,
        question["choices"][0]["message"],
        {"role": "tool", "tool_call_id": call["id"], "content": "pending"},
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": json.dumps({
                "results": [{"id": call["id"], "selectedOptions": ["Allow once"]}],
            }),
        },
    ]
    assert question_authorized_for_content(result_messages, "user_content") is True


def test_content_question_accepts_follow_up_user_selection():
    original_messages = [{"role": "user", "content": "Please execute the order."}]
    question_response_for_content(original_messages, "user_content", model="model")
    follow_up = [*original_messages, {"role": "user", "content": "Allow once"}]
    assert question_authorized_for_content(follow_up, "user_content") is True


def test_complete_content_guard_emits_native_question_then_accepts():
    response = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    tools = [{"type": "function", "function": {"name": "bash"}}]
    original_messages = [{"role": "user", "content": "Please delete the old file."}]
    question = _validate_complete_tool_response(
        response,
        tools,
        provider="provider",
        model="model",
        request_id="req-content-1",
        require_tool_call=False,
        reject_empty=False,
        content_regex=re.compile(r"delete", re.IGNORECASE),
        messages=original_messages,
        native_questions=True,
    )
    call = question["choices"][0]["message"]["tool_calls"][0]
    approved_messages = [
        *original_messages,
        question["choices"][0]["message"],
        {"role": "tool", "tool_call_id": call["id"], "content": "Allow once"},
    ]
    allowed = _validate_complete_tool_response(
        response,
        tools,
        provider="provider",
        model="model",
        request_id="req-content-2",
        require_tool_call=False,
        reject_empty=False,
        content_regex=re.compile(r"delete", re.IGNORECASE),
        messages=approved_messages,
        native_questions=True,
    )
    assert allowed == response


def test_question_result_cannot_authorize_changed_arguments():
    question = question_response_for_calls(_trade_call(qty=1), model="model")
    call = question["choices"][0]["message"]["tool_calls"][0]
    messages = [
        question["choices"][0]["message"],
        {"role": "tool", "tool_call_id": call["id"], "content": "Allow once"},
    ]
    assert question_authorized(messages, _trade_call(qty=2)) is False


def test_approval_audit_records_proposal_and_decision_without_raw_arguments():
    audit = _Audit()
    question = question_response_for_calls(
        _trade_call(),
        model="model",
        provider="provider",
        request_id="req-propose",
        audit=audit,
    )
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
    assert question_authorized(messages, _trade_call(), request_id="req-accept") is True
    assert [event["event_type"] for event in audit.events] == [
        "tool.approval.proposed",
        "tool.approval.decision",
    ]
    decision = audit.events[-1]
    assert decision["decision"] == "accepted"
    assert decision["execution_result"] == "not_observed"
    assert "arguments" not in decision
    assert "qty" not in json.dumps(audit.events)


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


@pytest.mark.asyncio
async def test_streaming_risky_user_content_is_replaced_with_question_tool():
    async def provider_stream():
        yield sse_frame({
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": "I can help."},
                "finish_reason": None,
            }],
        })
        yield sse_frame({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        })
        yield b"data: [DONE]\n\n"

    result = await _prepare_stream_result(
        provider_stream(),
        tools=[{"type": "function", "function": {"name": "bash"}}],
        tools_requested=True,
        provider="test",
        model="model",
        request_id="req-content-stream",
        content_regex=re.compile(r"delete", re.IGNORECASE),
        messages=[{"role": "user", "content": "Please delete the old file."}],
    )
    joined = b"".join([frame async for frame in result])
    assert b'"name": "ask"' in joined
    assert b"I can help" not in joined


@pytest.mark.asyncio
async def test_streaming_risky_user_content_precedes_guard_for_read_only_tool_call():
    async def provider_stream():
        yield sse_frame({
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-list",
                        "type": "function",
                        "function": {
                            "name": "list_files",
                            "arguments": "{}",
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        })
        yield b"data: [DONE]\n\n"

    result = await _prepare_stream_result(
        provider_stream(),
        tools=[{"type": "function", "function": {"name": "list_files"}}],
        tools_requested=True,
        provider="test",
        model="model",
        request_id="req-content-with-tool",
        content_regex=re.compile(r"delete", re.IGNORECASE),
        messages=[{"role": "user", "content": "Please delete the old file."}],
    )
    joined = b"".join([frame async for frame in result])
    assert b'"name": "ask"' in joined
    assert b'"name": "list_files"' not in joined
