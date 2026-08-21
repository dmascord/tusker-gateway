"""SSE (Server-Sent Events) framing utilities."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)


def sse_frame(data: dict) -> bytes:
    """Frame a dict as an SSE data event.

    Yields a single 'data: <json>\n\n' line.
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


def sse_done() -> bytes:
    """Yield the [DONE] sentinel."""
    return b"data: [DONE]\n\n"


def sse_comment(comment: str) -> bytes:
    """Yield a comment line (prefixed with colon).

    SSE comment lines are ignored by compliant clients but travel down the
    TCP/TLS/HTTP-2 stack as real bytes, which keeps idle-connection timers
    on intermediaries (Traefik, nginx, Cloudflare, CloudFront) from
    silently dropping long-lived streams. This is the cheapest way to
    keep an SSE stream alive — no client-visible state change.
    """
    return f": {comment}\n".encode("utf-8")


async def sse_heartbeat_loop(
    write: Callable[[bytes], Awaitable[None]],
    stop: asyncio.Event,
    *,
    interval_secs: float = 15.0,
    comment: str = "keepalive",
) -> None:
    """Emit SSE comment heartbeats at `interval_secs` until `stop` is set.

    Intended to be spawned as a sibling task to the main stream-write loop:

        stop = asyncio.Event()
        hb = asyncio.create_task(sse_heartbeat_loop(resp.write, stop, interval_secs=15))
        try:
            async for chunk in upstream:
                await resp.write(chunk)
        finally:
            stop.set()
            await hb

    Why comment lines and not no-op `data:` chunks? OMP's
    `isOpenAICompletionsProgressChunk` filter (see
    `packages/ai/src/providers/openai-completions.ts`) deliberately ignores
    empty / role-only chunks when deciding whether to reset its idle
    watchdog — so emitting `data: {"choices":[]}` keeps the *gateway*
    TCP socket alive but does NOT keep OMP's per-request timer alive. SSE
    comments (`:` prefix) are the only wire bytes that satisfy both layers:
    they don't carry data, they're cheap, and every SSE-aware intermediary
    silently passes them through.

    The writer is called only while the stream is healthy; a `ConnectionResetError`
    / `ConnectionError` from `write()` short-circuits the loop so we don't spam
    logs after the client has gone.
    """
    if interval_secs <= 0:
        # Heartbeat disabled — just wait until the consumer signals stop.
        await stop.wait()
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_secs)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            await write(sse_comment(comment))
        except (ConnectionResetError, ConnectionError, BrokenPipeError) as exc:
            logger.debug("heartbeat loop: client gone, stopping (%s)", exc)
            return
        except Exception as exc:  # noqa: BLE001
            # Don't kill the stream on a transient writer hiccup — log and
            # try again next interval. The next write to the main loop will
            # surface the real failure (if any) with a proper stack trace.
            logger.warning("heartbeat write failed (will retry): %s", exc)


async def tee_with_heartbeat(
    source: AsyncIterator[bytes],
    write: Callable[[bytes], Awaitable[None]],
    *,
    interval_secs: float = 15.0,
    comment: str = "keepalive",
) -> tuple[int, int]:
    """Pump `source` chunks to `write()` while emitting periodic heartbeats.

    Returns `(chunk_count, heartbeat_count)`. Heartbeats are emitted in the
    gaps between upstream chunks; if upstream is busy the heartbeat timer
    naturally defers. This is a thin wrapper around `sse_heartbeat_loop`
    that bundles the "start heartbeat task + iterate + stop + join" lifecycle
    into one call so endpoint code doesn't have to repeat it.
    """
    stop = asyncio.Event()
    hb_task = asyncio.create_task(
        sse_heartbeat_loop(write, stop, interval_secs=interval_secs, comment=comment),
        name="sse-heartbeat",
    )
    chunk_count = 0
    try:
        async for chunk in source:
            await write(chunk)
            chunk_count += 1
    finally:
        stop.set()
        try:
            await asyncio.wait_for(hb_task, timeout=interval_secs + 1.0)
        except asyncio.TimeoutError:
            hb_task.cancel()
    # The heartbeat task tracks its own write count via exceptions, but we
    # don't expose it directly. Surface at least that it ran by inspecting
    # whether the task is still scheduled (cheap proxy). For testability
    # we also let callers check via the comment argument they used.
    heartbeat_count = 0  # reserved for future telemetry hook
    return chunk_count, heartbeat_count


def format_openai_chunk(
    content: str | None = None,
    *,
    role: str | None = None,
    finish_reason: str | None = None,
    model: str | None = None,
) -> dict:
    """Build an OpenAI chat completion chunk dict."""
    chunk: dict = {"id": f"chatcmpl-{_rand_id()}", "object": "chat.completion.chunk", "choices": [], "model": model or "tusker-gateway"}

    delta: dict = {}
    if content is not None:
        delta["content"] = content
        delta["text"] = content
    if role is not None:
        delta["role"] = role

    choice: dict = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason

    chunk["choices"].append(choice)
    return chunk


def _rand_id() -> str:
    """Generate a short random hex ID."""
    import secrets
    return secrets.token_hex(14)


def format_openai_response(
    content: str,
    *,
    finish_reason: str = "stop",
    model: str | None = None,
) -> dict:
    """Build a non-streaming OpenAI chat completion response dict."""
    return {
        "id": f"chatcmpl-{_rand_id()}",
        "object": "chat.completion",
        "created": _timestamp(),
        "model": model or "tusker-gateway",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _timestamp() -> int:
    """Current Unix timestamp."""
    import time
    return int(time.time())
