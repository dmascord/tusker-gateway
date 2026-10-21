import hashlib
import json
import stat
from collections import OrderedDict

import pytest

from tusker_gateway.errors import ProviderError
from tusker_gateway.stream_diagnostics import (
    append_response_capture,
    extract_cycle_text,
    remember_upstream_request,
    store_reasoning_cycle,
    store_unusable_stream_capture,
    take_upstream_request,
)
from tusker_gateway.turn_capture import fetch_turn_capture, list_turn_captures


def test_reasoning_cycle_capture_is_private_and_contains_trigger_text(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_STREAM_DIAGNOSTICS_DIR", str(tmp_path / "private"))
    text = "repeat-me!" * 30
    name = store_reasoning_cycle(
        text,
        request_id="req-test",
        provider="provider-x",
        model="model-y",
        source="promoted_reasoning_content",
        cycle_chars=10,
        repeats=3,
    )

    assert name and name.startswith("cycle-")
    directory = tmp_path / "private"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    capture = directory / name
    assert stat.S_IMODE(capture.stat().st_mode) == 0o600
    record = json.loads(capture.read_text())
    assert record["text"] == text
    assert record["request_id"] == "req-test"
    assert record["source"] == "promoted_reasoning_content"


def test_cycle_capture_respects_record_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_STREAM_DIAGNOSTICS_DIR", str(tmp_path))
    import tusker_gateway.stream_diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "_MAX_RECORDS", 1)
    kwargs = dict(
        request_id=None, provider="p", model="m", source="reasoning_fields",
        cycle_chars=32, repeats=3,
    )
    assert store_reasoning_cycle("x" * 96, **kwargs)
    assert store_reasoning_cycle("y" * 96, **kwargs) is None


def test_extract_cycle_text_clips_to_capture_bound():
    assert extract_cycle_text("a" * 5000, 1024, 8) == "a" * 4096


def _isolate_pending(monkeypatch):
    """The capture store is module-global; start from a clean dict."""
    import tusker_gateway.stream_diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "_PENDING", OrderedDict())
    return diagnostics


def _enable_capture(tmp_path, monkeypatch):
    """Point rejected-turn capture at a throwaway database."""
    monkeypatch.setenv("TUSKER_STREAM_CAPTURE", "1")
    monkeypatch.setenv("TUSKER_TURN_CAPTURE_PATH", str(tmp_path / "turn-captures.db"))
    # Keep a shared-state DSN out of the way so the sqlite file is used.
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    import tusker_gateway.turn_capture as turn_capture

    monkeypatch.setattr(turn_capture, "_STORE", None)
    monkeypatch.setattr(turn_capture, "_STORE_CONFIG", None)


def _remember(*, request_id="req-42", payload=b'{"model":"m"}'):
    remember_upstream_request(
        request_id=request_id,
        provider="alibaba",
        model="deepseek-v4.1-flash",
        pool="code",
        stream=True,
        source="chat_completions",
        payload=payload,
    )


def test_rejected_turn_capture_is_opt_in(tmp_path, monkeypatch):
    """Prompt bytes are held and written only when the operator enables it."""
    monkeypatch.setenv("TUSKER_TURN_CAPTURE_PATH", str(tmp_path / "turn-captures.db"))
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    monkeypatch.delenv("TUSKER_STREAM_CAPTURE", raising=False)
    diagnostics = _isolate_pending(monkeypatch)

    remember_upstream_request(
        request_id="req-off",
        provider="alibaba",
        model="m",
        pool="code",
        stream=True,
        source="chat_completions",
        payload=b'{"messages":[{"content":"prompt"}]}',
    )

    assert not diagnostics._PENDING
    name = store_unusable_stream_capture(
        request_id="req-off",
        provider="alibaba",
        model="m",
        reason="empty_stream",
        response=b"data: [DONE]\n\n",
        verdict={},
    )
    assert name is None
    # The capture database is never even opened while the switch is off.
    assert not (tmp_path / "turn-captures.db").exists()


def test_capture_holds_only_keyed_streaming_turns(tmp_path, monkeypatch):
    """A non-streaming or unkeyed turn is never pinned in memory."""
    _enable_capture(tmp_path, monkeypatch)
    diagnostics = _isolate_pending(monkeypatch)

    remember_upstream_request(
        request_id="req-nostream",
        provider="p",
        model="m",
        pool=None,
        stream=False,
        source="chat_completions",
        payload=b"{}",
    )
    remember_upstream_request(
        request_id=None,
        provider="p",
        model="m",
        pool=None,
        stream=True,
        source="chat_completions",
        payload=b"{}",
    )

    assert not diagnostics._PENDING


def test_rejected_turn_capture_records_request_response_and_verdict(tmp_path, monkeypatch):
    """Both sides of a rejected turn are kept byte-for-byte, with the verdict."""
    _enable_capture(tmp_path, monkeypatch)
    request_body = json.dumps({
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "read the file"}],
        "stream": True,
    }).encode()
    response = (
        b'data: {"choices":[{"index":0,"delta":{"role":"assistant"},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"choices":[],"finish_reason":"stop"}\n\n'
        b"data: [DONE]\n\n"
    )
    _remember(payload=request_body)

    capture_id = store_unusable_stream_capture(
        request_id="req-42",
        provider="alibaba",
        model="deepseek-v4.1-flash",
        reason="reasoning_only_or_empty",
        response=response,
        verdict={"content_chars": 0, "reasoning_chars": 12},
    )

    assert capture_id
    record = fetch_turn_capture(capture_id)
    assert record is not None
    assert record["request_id"] == "req-42"
    assert record["provider"] == "alibaba"
    assert record["model"] == "deepseek-v4.1-flash"
    assert record["pool"] == "code"
    assert record["request_source"] == "chat_completions"
    assert record["reason"] == "reasoning_only_or_empty"
    assert record["verdict"] == {"content_chars": 0, "reasoning_chars": 12}
    assert record["body_policy"] == "captured"
    assert record["request_body"] == request_body
    assert record["request_bytes"] == len(request_body)
    assert record["request_sha256"] == hashlib.sha256(request_body).hexdigest()
    assert record["request_truncated_bytes"] == 0
    assert record["response_body"] == response
    assert record["response_bytes"] == len(response)
    assert record["response_sha256"] == hashlib.sha256(response).hexdigest()
    assert record["response_truncated_bytes"] == 0
    # A byte-complete request is what makes an offline replay faithful.
    assert record["replayable"] is True

    # The pending request is consumed, so a rejected turn cannot leak its prompt
    # into the next record for the same id.
    assert take_upstream_request("req-42") is None
    second = store_unusable_stream_capture(
        request_id="req-42",
        provider="alibaba",
        model="deepseek-v4.1-flash",
        reason="empty_stream",
        response=b"",
        verdict={},
    )
    second_record = fetch_turn_capture(second)
    assert second_record["request_id"] == "req-42"
    assert second_record["request_source"] == "unavailable"
    # The consumed hold leaves the pool unknown for this second rejection, so
    # the row records that no pool could be established rather than storing
    # bytes for a pool whose ZDR policy was never proven.
    assert second_record["pool"] is None
    assert second_record["request_body"] is None
    assert second_record["body_policy"] == "unknown_pool"
    assert second_record["replayable"] is False
    assert second_record["response_bytes"] is None


def test_rejected_turn_capture_refuses_to_pin_an_oversize_prompt(tmp_path, monkeypatch):
    """An oversize request keeps its verdict but never its bytes.

    The hold is what bounds gateway memory, so an oversize prompt is dropped at
    the hold rather than stored as an excerpt: a partial request would replay as
    a different request.
    """
    _enable_capture(tmp_path, monkeypatch)
    monkeypatch.setenv("TUSKER_TURN_CAPTURE_MAX_REQUEST_BYTES", "16384")
    monkeypatch.setenv("TUSKER_TURN_CAPTURE_MAX_RESPONSE_BYTES", "65536")
    payload = b"a" * 32_768
    response = b"b" * 70_000
    _remember(payload=payload)

    capture_id = store_unusable_stream_capture(
        request_id="req-42",
        provider="alibaba",
        model="m",
        reason="empty_stream",
        response=response,
        verdict={},
    )

    record = fetch_turn_capture(capture_id)
    assert record["body_policy"] == "request_unavailable"
    assert record["request_body"] is None
    assert record["request_bytes"] is None
    assert record["replayable"] is False
    # The response side is still kept, bounded by its own limit.
    assert record["response_bytes"] == 70_000
    assert record["response_sha256"] == hashlib.sha256(response).hexdigest()
    assert record["response_truncated_bytes"] == 70_000 - 65_536
    assert b"TUSKER-CAPTURE-TRUNCATED" in record["response_body"]


def test_response_capture_stops_at_the_bound():
    capture = bytearray()
    append_response_capture(capture, b"x" * 65_536)
    append_response_capture(capture, b"y" * 1024)

    assert len(capture) == 65_536
    assert append_response_capture(None, b"z") is None


class _FakeUpstreamResponse:
    """Minimal stand-in for an ``aiohttp.ClientResponse`` error reply."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body
        self.headers: dict[str, str] = {}

    async def text(self) -> str:
        return self._body


def test_non_2xx_response_captures_the_request_that_was_rejected(tmp_path, monkeypatch):
    """A 400 leaves the exact outbound request in the state DB.

    ``mlx-mac`` rejected two attempts with ``invalid tool call arguments`` and
    the only trace was a truncated log line, so the request bytes that produced
    the verdict were unrecoverable. ``_check_response`` now stores both sides
    through the same rejected-turn capture used for stream failures.
    """
    import asyncio

    from tusker_gateway.passthrough import PassthroughClient

    _enable_capture(tmp_path, monkeypatch)
    _isolate_pending(monkeypatch)
    _remember(request_id="req-400", payload=b'{"model":"qwen3.8-27b","tools":[]}')

    body = '{"error":{"message":"invalid tool call arguments"}}'
    with pytest.raises(ProviderError):
        asyncio.run(
            PassthroughClient._check_response(
                _FakeUpstreamResponse(400, body),
                provider="mlx-mac",
                model="qwen3.8-27b",
                request_id="req-400",
                pool="code",
                request_payload=b'{"model":"qwen3.8-27b","tools":[]}',
            )
        )

    captures = list_turn_captures(limit=10)
    assert len(captures) == 1
    record = fetch_turn_capture(captures[0]["capture_id"])
    assert record["reason"] == "http_400"
    assert record["provider"] == "mlx-mac"
    assert record["body_policy"] == "captured"
    assert json.loads(record["request_body"])["model"] == "qwen3.8-27b"
    assert b"invalid tool call arguments" in record["response_body"]
    # The hold is consumed, so a retry cannot attach the same bytes twice.
    assert take_upstream_request("req-400") is None


def test_non_2xx_capture_withholds_bytes_for_the_privacy_pool(tmp_path, monkeypatch):
    """The ZDR pool keeps its verdict but never persists prompt bytes."""
    import asyncio

    from tusker_gateway.passthrough import PassthroughClient

    _enable_capture(tmp_path, monkeypatch)
    _isolate_pending(monkeypatch)
    remember_upstream_request(
        request_id="req-zdr",
        provider="mlx-mac",
        model="qwen3.8-27b",
        pool="privacy",
        stream=False,
        source="chat_completions",
        payload=b'{"messages":[{"content":"private"}]}',
    )

    with pytest.raises(ProviderError):
        asyncio.run(
            PassthroughClient._check_response(
                _FakeUpstreamResponse(400, "{}"),
                provider="mlx-mac",
                model="qwen3.8-27b",
                request_id="req-zdr",
                pool="privacy",
                request_payload=b'{"messages":[{"content":"private"}]}',
            )
        )

    captures = list_turn_captures(limit=10)
    assert len(captures) == 1
    record = fetch_turn_capture(captures[0]["capture_id"])
    assert record["body_policy"] == "excluded_pool"
    assert record["request_body"] is None
    assert record["response_body"] is None


def test_non_2xx_capture_is_opt_in(tmp_path, monkeypatch):
    """With the switch off a rejection writes nothing."""
    import asyncio

    from tusker_gateway.passthrough import PassthroughClient

    monkeypatch.setenv("TUSKER_TURN_CAPTURE_PATH", str(tmp_path / "turn-captures.db"))
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    monkeypatch.delenv("TUSKER_STREAM_CAPTURE", raising=False)
    _isolate_pending(monkeypatch)
    _remember(request_id="req-off-400")

    with pytest.raises(ProviderError):
        asyncio.run(
            PassthroughClient._check_response(
                _FakeUpstreamResponse(400, "{}"),
                provider="mlx-mac",
                model="m",
                request_id="req-off-400",
                pool="code",
            )
        )

    assert not (tmp_path / "turn-captures.db").exists()
