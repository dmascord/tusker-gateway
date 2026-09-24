from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from tusker_gateway.errors import ProviderError
from tusker_gateway.provider_adapters.cli_streaming import (
    claude_text_from_event,
    stream_cli_jsonl,
    text_from_event,
)
from tusker_gateway.sse import sse_data_payload


class _Writer:
    def write(self, _data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _Reader:
    def __init__(self, lines):
        self.lines = list(lines) + [b""]

    async def readline(self):
        if not self.lines:
            return b""
        return self.lines.pop(0)

    async def read(self):
        return b""


class _Process:
    pid = 123
    returncode = 0

    def __init__(self, lines):
        self.stdin = _Writer()
        self.stdout = _Reader(lines)
        self.stderr = _Reader([])

    async def wait(self):
        return self.returncode


class _StderrReader:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self):
        return self._data


@pytest.mark.parametrize("event,extractor", [
    ({"type": "step_start", "part": {}}, text_from_event),
    ({"type": "tool_call", "part": {"text": "hidden diagnostics"}}, text_from_event),
    ({"type": "stream_event", "event": {"type": "message_start"}}, claude_text_from_event),
])
def test_cli_event_filter_ignores_non_assistant_text(event, extractor):
    assert extractor(event) is None


@pytest.mark.asyncio
async def test_jsonl_cli_stream_emits_text_delta_before_finish():
    process = _Process([
        json.dumps({"type": "step_start"}).encode() + b"\n",
        json.dumps({"type": "text", "part": {"text": "first"}}).encode() + b"\n",
        json.dumps({"type": "text", "part": {"text": " second"}}).encode() + b"\n",
    ])
    stream = stream_cli_jsonl(
        ["fake-cli"], env={}, prompt=b"prompt", model="kilo-cli/kilo/kilo-auto/free",
        timeout=1, text_extractor=text_from_event,
    )
    with patch(
        "tusker_gateway.provider_adapters.cli_streaming.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        first = await anext(stream)
        first_chunk = json.loads(sse_data_payload(first))
        assert first_chunk["choices"][0]["delta"]["content"] == "first"
        tail = [item async for item in stream]

    decoded = [json.loads(sse_data_payload(item)) for item in tail[:-1]]
    assert decoded[0]["choices"][0]["delta"]["content"] == " second"
    assert decoded[-1]["choices"][0]["finish_reason"] == "stop"
    assert tail[-1] == b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_claude_partial_text_delta_is_forwarded_but_other_events_are_not():
    event = {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "token"},
        },
    }
    process = _Process([json.dumps(event).encode() + b"\n"])
    stream = stream_cli_jsonl(
        ["fake-claude"], env={}, prompt=b"prompt", model="claude-code-cli/sonnet",
        timeout=1, text_extractor=claude_text_from_event,
    )
    with patch(
        "tusker_gateway.provider_adapters.cli_streaming.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        frames = [item async for item in stream]
    assert json.loads(sse_data_payload(frames[0]))["choices"][0]["delta"]["content"] == "token"


@pytest.mark.asyncio
async def test_nonzero_cli_exit_logs_bounded_stderr_preview():
    stderr = b"cli: fatal auth error\n" + b"x" * 600
    process = _Process([])
    process.returncode = 1
    process.stderr = _StderrReader(stderr)
    stream = stream_cli_jsonl(
        ["fake-cli"], env={}, prompt=b"prompt", model="kilo-cli/kilo/kilo-auto/free",
        timeout=1, text_extractor=text_from_event, error_code="kilo_cli_failed",
    )
    with patch(
        "tusker_gateway.provider_adapters.cli_streaming.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ), patch("tusker_gateway.provider_adapters.cli_streaming.logger") as log:
        with pytest.raises(ProviderError) as exc:
            [item async for item in stream]

    assert exc.value.code == "kilo_cli_failed"
    (message, rc, size, preview), _ = log.warning.call_args
    assert message == "CLI stream exited rc=%s stderr_bytes=%d stderr=%r"
    assert (rc, size) == (1, len(stderr))
    assert preview == stderr.decode("utf-8")[:512]
    assert len(preview) == 512
