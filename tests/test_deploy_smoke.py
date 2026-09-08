"""Regression tests for fail-closed deployment chat smoke checks."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_K8S = Path(__file__).resolve().parent.parent / "k8s"

def _load_module(name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, _K8S / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke_chat = _load_module("smoke_chat")


def _chunk(data: bytes, size: int) -> list[bytes]:
    """Split bytes into chunks of at most ``size`` bytes (simulates reads)."""
    return [data[i : i + size] for i in range(0, len(data), size)]


def _frame(payload: str) -> bytes:
    return f"data: {payload}\n\n".encode("utf-8")


def _chunk_frame(payload: str) -> bytes:
    """An OpenAI chat-completion content chunk."""
    return _frame(json.dumps({
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": payload}, "finish_reason": None}],
    }))


# ---------------------------------------------------------------------------
# smoke_chat: truncated stream
# ---------------------------------------------------------------------------

def test_truncated_stream_without_done_fails():
    """A stream that never emits [DONE] must fail (not silently pass)."""
    stream = [_chunk_frame("hello"), _chunk_frame(" world")]
    result = smoke_chat.check_stream(iter(stream))
    assert result["ok"] is False
    assert "DONE" in result["reason"]


def test_empty_stream_fails():
    """No frames at all must fail closed."""
    result = smoke_chat.check_stream(iter([]))
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# smoke_chat: valid fragmented stream
# ---------------------------------------------------------------------------

def test_valid_fragmented_stream_succeeds():
    """A valid stream split across many read chunks is accepted."""
    full = _chunk_frame("hello") + _chunk_frame(" world") + _frame("[DONE]")
    chunks = _chunk(full, 7)  # force boundaries to split mid-frame
    result = smoke_chat.check_stream(iter(chunks))
    assert result["ok"] is True
    assert result["content"] == "hello world"


def test_fragmented_crlf_frames_succeed():
    """CRLF-framed stream (as some providers emit) is accepted."""
    frame = b"data: {\"choices\":[{\"delta\":{\"content\":\"x\"}}]}\r\n\r\n"
    full = frame + b"data: [DONE]\r\n\r\n"
    result = smoke_chat.check_stream(iter(_chunk(full, 5)))
    assert result["ok"] is True
    assert result["content"] == "x"


# ---------------------------------------------------------------------------
# smoke_chat: error / no useful output
# ---------------------------------------------------------------------------

def test_stream_error_event_fails():
    """An error event in the stream must fail, not be masked."""
    stream = [
        _frame(json.dumps({"error": {"message": "no live providers", "type": "upstream"}})),
        _frame("[DONE]"),
    ]
    result = smoke_chat.check_stream(iter(stream))
    assert result["ok"] is False
    assert "error event" in result["reason"]


def test_no_live_provider_no_content_fails():
    """[DONE] with no assistant content (e.g. no live providers) must fail."""
    stream = [_frame("[DONE]")]
    result = smoke_chat.check_stream(iter(stream))
    assert result["ok"] is False
    assert "no nonempty assistant content" in result["reason"]


# ---------------------------------------------------------------------------
# smoke_chat: HTTP failures
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, _size: int) -> bytes:
        body = self._body
        self._body = b""
        return body


def test_fetch_chat_non_2xx_fails(monkeypatch):
    """A non-2xx HTTP response must fail, not be treated as success."""
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 503, "Service Unavailable", {}, None
        )
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = smoke_chat.fetch_chat("https://x.test/v1/chat", "sk-test", "hermes-code")
    assert result["ok"] is False
    assert "503" in result["reason"]


def test_fetch_chat_missing_key_fails():
    """A missing API key must fail, not be skipped."""
    result = smoke_chat.fetch_chat("https://x.test/v1/chat", "", "hermes-code")
    assert result["ok"] is False
    assert "missing API key" in result["reason"]


def test_fetch_chat_valid_stream_succeeds(monkeypatch):
    """A healthy stream from the gateway is accepted end-to-end."""
    body = _chunk_frame("DONE") + _frame("[DONE]")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _FakeResponse(200, body))
    result = smoke_chat.fetch_chat("https://x.test/v1/chat", "sk-test", "hermes-code")
    assert result["ok"] is True
    assert result["content"] == "DONE"
    assert result["http_status"] == 200


def test_named_error_after_content_fails():
    result = smoke_chat.check_stream([
        _chunk_frame("hello"), b'event: error\ndata: {"message":"failed"}\n\n', _frame("[DONE]")
    ])
    assert result["ok"] is False


def test_malformed_event_after_content_fails():
    assert smoke_chat.check_stream([_chunk_frame("hello"), _frame("broken"), _frame("[DONE]")])["ok"] is False


def test_partial_tail_after_done_fails():
    assert smoke_chat.check_stream([_chunk_frame("hello"), _frame("[DONE]"), b"data: {"])["ok"] is False


def test_total_stream_limit_applies_across_frames(monkeypatch):
    monkeypatch.setattr(smoke_chat, "MAX_STREAM_BYTES", 300)
    assert smoke_chat.check_stream([_chunk_frame("hello")] * 10 + [_frame("[DONE]")])["ok"] is False


