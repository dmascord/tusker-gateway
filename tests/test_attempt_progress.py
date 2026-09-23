"""Buffered Codex parsing must count as progress for the attempt watchdog.

Regression for req_7b0d460047e30296: Codex responses are assembled from SSE
into one dict before the endpoint sees anything, so a healthy Codex turn
longer than ``TUSKER_PROVIDER_ATTEMPT_TIMEOUT_SECS`` was killed as "idle".
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from tusker_gateway.endpoints import _AttemptActivity, _await_attempt
from tusker_gateway.passthrough import PassthroughClient, bind_upstream_progress


class _SlowChunks:
    """Async iterator yielding SSE lines with a delay before each one."""

    def __init__(self, lines: list[bytes], delay: float) -> None:
        self._lines = list(lines)
        self._delay = delay

    def __aiter__(self) -> "_SlowChunks":
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        await asyncio.sleep(self._delay)
        return self._lines.pop(0)


def _slow_codex_response() -> MagicMock:
    events = [{"type": "response.created"}]
    events += [
        {"type": "response.reasoning_summary_text.delta", "delta": "thinking "}
        for _ in range(6)
    ]
    events += [
        {"type": "response.output_text.delta", "delta": "done"},
        {"type": "response.completed", "response": {"usage": {"output_tokens": 1}}},
    ]
    response = MagicMock()
    # 9 events x 0.05s = 0.45s total, well past the 0.2s idle budget.
    response.content = _SlowChunks(
        [f"data: {json.dumps(event)}\n".encode() for event in events],
        delay=0.05,
    )
    return response


async def test_codex_sse_events_extend_idle_budget():
    client = PassthroughClient.__new__(PassthroughClient)
    activity = _AttemptActivity()

    async def attempt():
        bind_upstream_progress(activity.touch)
        return await client._parse_codex_sse_async(_slow_codex_response())

    result = await _await_attempt(
        asyncio.create_task(attempt()),
        budget=0.2,
        deadline_at=None,
        activity=activity,
    )

    assert result["choices"][0]["message"]["content"] == "done"


async def test_unbound_buffered_attempt_still_times_out():
    client = PassthroughClient.__new__(PassthroughClient)
    activity = _AttemptActivity()

    with pytest.raises(asyncio.TimeoutError):
        await _await_attempt(
            asyncio.create_task(client._parse_codex_sse_async(_slow_codex_response())),
            budget=0.2,
            deadline_at=None,
            activity=activity,
        )
