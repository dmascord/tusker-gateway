"""Tests for SSE framing, OpenAI object builders, and SSE keepalive/heartbeat helpers."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tusker_gateway.sse import (
    format_openai_chunk,
    format_openai_response,
    sse_comment,
    sse_done,
    sse_frame,
    sse_heartbeat_loop,
    tee_with_heartbeat,
)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def test_sse_framing():
    data = {"foo": "bar"}
    frame = sse_frame(data)
    assert frame == b'data: {"foo": "bar"}\n\n'
    assert sse_done() == b"data: [DONE]\n\n"


def test_sse_comment_includes_colon_prefix():
    """Comment lines must start with ': ' so SSE parsers ignore them.

    This is the whole point: comments travel down the TCP stack as real
    bytes (so idle-connection timers don't fire) but are stripped by every
    conformant SSE parser without affecting the parsed event stream.
    """
    assert sse_comment("keepalive") == b": keepalive\n"
    assert sse_comment("").startswith(b": ")
    assert not sse_comment("anything").startswith(b"data: ")


def test_sse_frame_handles_non_ascii():
    """ensure_ascii=False keeps UTF-8 intact for non-ASCII content."""
    payload = sse_frame({"content": "héllo 🦀"})
    assert "🦀".encode() in payload


def test_format_openai_chunk():
    chunk = format_openai_chunk(content="hello", role="assistant")
    assert "choices" in chunk
    assert chunk["choices"][0]["delta"]["content"] == "hello"
    assert chunk["choices"][0]["delta"]["role"] == "assistant"
    assert chunk["model"] == "tusker-gateway"


def test_format_openai_response():
    resp = format_openai_response(content="full text", model="my-model")
    assert resp["object"] == "chat.completion"
    assert resp["choices"][0]["message"]["content"] == "full text"
    assert resp["model"] == "my-model"


# ---------------------------------------------------------------------------
# Heartbeat loop
# ---------------------------------------------------------------------------

class _RecordingWriter:
    """Minimal async sink for the heartbeat loop. Records every write()."""
    def __init__(self, fail_on: tuple[int, ...] = ()) -> None:
        self.writes: list[bytes] = []
        self._fail_on = fail_on
        self._call_count = 0

    async def __call__(self, data: bytes) -> None:
        self._call_count += 1
        if self._call_count in self._fail_on:
            raise ConnectionResetError("simulated client gone")
        self.writes.append(data)


@pytest.mark.asyncio
async def test_heartbeat_emits_periodic_comments_until_stopped():
    writer = _RecordingWriter()
    stop = asyncio.Event()
    # 0.05s interval keeps the test fast while still proving the loop runs.
    task = asyncio.create_task(sse_heartbeat_loop(writer, stop, interval_secs=0.05))
    try:
        # Let it run for ~0.2s -> expect ~3-5 comments.
        await asyncio.sleep(0.22)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert len(writer.writes) >= 2, f"expected >=2 heartbeats, got {len(writer.writes)}"
    for w in writer.writes:
        assert w.startswith(b": "), f"non-comment write leaked: {w!r}"
        assert w.endswith(b"\n")


@pytest.mark.asyncio
async def test_heartbeat_exits_immediately_when_stop_already_set():
    """A pre-set stop must short-circuit the loop without writing anything."""
    writer = _RecordingWriter()
    stop = asyncio.Event()
    stop.set()
    await sse_heartbeat_loop(writer, stop, interval_secs=0.01)
    assert writer.writes == []


@pytest.mark.asyncio
async def test_heartbeat_disabled_when_interval_is_zero():
    """TUSKER_SSE_HEARTBEAT_SECS=0 (or negative) means no heartbeats — just wait."""
    writer = _RecordingWriter()
    stop = asyncio.Event()
    task = asyncio.create_task(sse_heartbeat_loop(writer, stop, interval_secs=0))
    # Even after sleeping the loop should not have written anything.
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert writer.writes == []


@pytest.mark.asyncio
async def test_heartbeat_stops_when_writer_raises_connection_reset():
    """ConnectionResetError from the writer means the client is gone — stop silently."""
    writer = _RecordingWriter(fail_on=(1,))  # fail on the very first heartbeat
    stop = asyncio.Event()
    task = asyncio.create_task(sse_heartbeat_loop(writer, stop, interval_secs=0.02))
    await asyncio.wait_for(task, timeout=1.0)
    # We must not have crashed, and we must not have looped forever.
    assert not task.cancelled()
    assert writer.writes == []  # the failing write is never recorded


@pytest.mark.asyncio
async def test_heartbeat_continues_through_transient_writer_failure():
    """Generic exceptions are logged + loop continues (next interval retries)."""
    # Build a writer that fails once with a non-fatal exception, then succeeds.
    calls = {"n": 0}

    async def writer(data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        # otherwise drop on the floor

    stop = asyncio.Event()
    task = asyncio.create_task(sse_heartbeat_loop(writer, stop, interval_secs=0.02))
    try:
        await asyncio.sleep(0.1)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
    # We made it through at least 2 ticks despite the first one raising.
    assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# tee_with_heartbeat (combined helper)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tee_pumps_all_chunks_and_stops_heartbeat_after_done():
    writer = _RecordingWriter()
    upstream = _async_iter([b"data: hello\n\n", b"data: world\n\n"])

    chunk_count, _ = await tee_with_heartbeat(
        upstream,
        writer,
        interval_secs=0.05,
        comment="keepalive",
    )
    assert chunk_count == 2
    # The two upstream bytes are forwarded verbatim.
    assert b"data: hello" in writer.writes[0]
    assert b"data: world" in writer.writes[1]


@pytest.mark.asyncio
async def test_tee_emits_heartbeats_during_idle_upstream():
    """If the upstream pauses, heartbeats should still go out."""
    writer = _RecordingWriter()
    # 1 chunk now, then sleep forever — so the only way to end the test is to
    # cancel the consumer task. Heartbeats must keep firing in the gap.
    async def slow_upstream():
        yield b"first\n\n"
        await asyncio.Event().wait()  # never set -> suspends forever

    task = asyncio.create_task(
        tee_with_heartbeat(slow_upstream(), writer, interval_secs=0.05, comment="hb")
    )
    try:
        # Wait long enough for: 1 chunk + several heartbeat ticks.
        await asyncio.sleep(0.25)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    writes = writer.writes
    has_chunk = any(b.startswith(b"first") for b in writes)
    has_hb = any(b.startswith(b": hb") for b in writes)
    assert has_chunk, f"upstream chunk missing: {writes!r}"
    assert has_hb, f"heartbeats missing during idle upstream: {writes!r}"


async def _async_iter(items: list[bytes], *, idle_before_each: float = 0.0):
    for it in items:
        if idle_before_each:
            await asyncio.sleep(idle_before_each)
        yield it
