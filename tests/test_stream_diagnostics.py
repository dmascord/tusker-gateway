import hashlib
import json
import stat
from collections import OrderedDict

from tusker_gateway.stream_diagnostics import (
    append_response_capture,
    extract_cycle_text,
    remember_upstream_request,
    store_reasoning_cycle,
    store_unusable_stream_capture,
    take_upstream_request,
)


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
    monkeypatch.setenv("TUSKER_STREAM_DIAGNOSTICS_DIR", str(tmp_path / "captures"))
    monkeypatch.setenv("TUSKER_STREAM_CAPTURE", "1")


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
    monkeypatch.setenv("TUSKER_STREAM_DIAGNOSTICS_DIR", str(tmp_path / "captures"))
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
    assert not (tmp_path / "captures").exists()


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

    name = store_unusable_stream_capture(
        request_id="req-42",
        provider="alibaba",
        model="deepseek-v4.1-flash",
        reason="reasoning_only_or_empty",
        response=response,
        verdict={"content_chars": 0, "reasoning_chars": 12},
    )

    assert name and name.startswith("unusable-") and name.endswith(".json")
    capture = tmp_path / "captures" / name
    assert stat.S_IMODE(capture.stat().st_mode) == 0o600
    record = json.loads(capture.read_text())
    assert record["request_id"] == "req-42"
    assert record["provider"] == "alibaba"
    assert record["model"] == "deepseek-v4.1-flash"
    assert record["pool"] == "code"
    assert record["request_source"] == "chat_completions"
    assert record["reason"] == "reasoning_only_or_empty"
    assert record["verdict"] == {"content_chars": 0, "reasoning_chars": 12}
    assert record["upstream_request"]["bytes"] == len(request_body)
    assert record["upstream_request"]["sha256"] == hashlib.sha256(request_body).hexdigest()
    assert record["upstream_request"]["text"] == request_body.decode()
    assert record["upstream_response"]["bytes"] == len(response)
    assert record["upstream_response"]["sha256"] == hashlib.sha256(response).hexdigest()
    assert record["upstream_response"]["text"] == response.decode()
    assert record["upstream_response"]["truncated_bytes"] == 0

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
    second_record = json.loads((tmp_path / "captures" / second).read_text())
    assert second_record["request_source"] == "unavailable"
    assert second_record["upstream_request"] is None


def test_rejected_turn_capture_bounds_large_payloads(tmp_path, monkeypatch):
    """A giant prompt or response is truncated, not written whole."""
    _enable_capture(tmp_path, monkeypatch)
    payload = b"a" * 32_768
    response = b"b" * 70_000
    _remember(payload=payload)

    name = store_unusable_stream_capture(
        request_id="req-42",
        provider="alibaba",
        model="m",
        reason="empty_stream",
        response=response,
        verdict={},
    )

    record = json.loads((tmp_path / "captures" / name).read_text())
    captured_request = record["upstream_request"]
    assert captured_request["bytes"] == 32_768
    assert captured_request["sha256"] == hashlib.sha256(payload).hexdigest()
    assert captured_request["truncated_bytes"] == 32_768 - 16_384
    assert "TUSKER-CAPTURE-TRUNCATED" in captured_request["text"]
    captured_response = record["upstream_response"]
    assert captured_response["bytes"] == 70_000
    assert captured_response["sha256"] == hashlib.sha256(response).hexdigest()
    assert captured_response["truncated_bytes"] == 70_000 - 65_536


def test_response_capture_stops_at_the_bound():
    capture = bytearray()
    append_response_capture(capture, b"x" * 65_536)
    append_response_capture(capture, b"y" * 1024)

    assert len(capture) == 65_536
    assert append_response_capture(None, b"z") is None


def test_store_budget_ignores_unrelated_files_in_the_capture_directory(tmp_path, monkeypatch):
    """Only this module's captures count against the shared store budget.

    pytest writes unrelated files into its tmp_path, and operators may drop
    their own notes beside the captures; neither may silently disable capture.
    """
    _enable_capture(tmp_path, monkeypatch)
    import tusker_gateway.stream_diagnostics as diagnostics

    root = tmp_path / "captures"
    root.mkdir()
    (root / "auth.json").write_text("{}")
    monkeypatch.setattr(diagnostics, "_MAX_RECORDS", 1)
    _remember()

    assert store_unusable_stream_capture(
        request_id="req-42",
        provider="alibaba",
        model="m",
        reason="empty_stream",
        response=b"data: [DONE]\n\n",
        verdict={},
    )
    _remember(request_id="req-43")
    assert store_unusable_stream_capture(
        request_id="req-43",
        provider="alibaba",
        model="m",
        reason="empty_stream",
        response=b"data: [DONE]\n\n",
        verdict={},
    ) is None
