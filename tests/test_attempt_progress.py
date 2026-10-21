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

from tusker_gateway.endpoints import (
    _AttemptActivity,
    _await_attempt,
    _provider_attempt_timeout_secs,
)
from tusker_gateway.passthrough import (
    PassthroughClient,
    _non_stream_total_secs,
    _stream_total_budget,
    _upstream_idle_secs,
    bind_upstream_progress,
)


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


def test_upstream_idle_window_never_preempts_attempt_budget(monkeypatch):
    """A read timer shorter than the attempt budget reports a timeout the
    budget was configured to prevent.

    Production evidence, req_e1d8a9fe5ade8736 (2026-10-21): ``mlx-mac``
    carries a 240s attempt budget, but the flat 90s socket timer fired first
    on a cold 240K-token prompt, so the operator-visible budget never
    applied.
    """
    monkeypatch.setenv("TUSKER_UPSTREAM_SOCK_READ_SECS", "90")
    monkeypatch.setenv(
        "TUSKER_PROVIDER_ATTEMPT_TIMEOUT_OVERRIDES_JSON",
        '{"mlx-mac": 240, "ollama-cloud": 180}',
    )
    monkeypatch.setenv(
        "TUSKER_PROVIDER_STREAM_TIMEOUT_OVERRIDES_JSON", '{"xiaomi": 600}'
    )

    for provider in ("mlx-mac", "ollama-cloud", "xiaomi"):
        attempt = _provider_attempt_timeout_secs(None, provider=provider, model="m")
        idle = _upstream_idle_secs(provider, "m")
        assert idle >= attempt, f"{provider}: idle {idle}s < attempt {attempt}s"
        assert _stream_total_budget(provider=provider) >= attempt


def test_upstream_idle_window_defaults_to_the_flat_socket_value(monkeypatch):
    monkeypatch.setenv("TUSKER_UPSTREAM_SOCK_READ_SECS", "90")
    monkeypatch.setenv("TUSKER_PROVIDER_ATTEMPT_TIMEOUT_OVERRIDES_JSON", "")
    monkeypatch.setenv("TUSKER_PROVIDER_STREAM_TIMEOUT_OVERRIDES_JSON", "")

    assert _upstream_idle_secs("google", "gemini") == 90.0
    assert _stream_total_budget(provider="google") == 90.0


def test_model_scoped_attempt_override_raises_the_upstream_window(monkeypatch):
    monkeypatch.setenv("TUSKER_UPSTREAM_SOCK_READ_SECS", "90")
    monkeypatch.setenv(
        "TUSKER_PROVIDER_ATTEMPT_TIMEOUT_OVERRIDES_JSON",
        '{"alibaba/qwen3.8-max": 120, "alibaba": 60}',
    )

    assert _upstream_idle_secs("alibaba", "qwen3.8-max") == 120.0
    # A provider override below the socket floor does not shorten it.
    assert _upstream_idle_secs("alibaba", "other") == 90.0
    assert _upstream_idle_secs("google", "gemini") == 90.0


def test_non_stream_total_covers_the_configured_attempt_budget(monkeypatch):
    """A non-stream route must not die before its attempt budget.

    Production evidence (2026-10-21): non-stream ``local-llm/qwen3-vl:8b``
    failed at 120.4s and again at 121.0s even though that provider carries a
    240s attempt budget for cold model loads. The flat 120s ``total`` fired
    first, so the configured budget never applied.
    """
    monkeypatch.setenv("TUSKER_UPSTREAM_SOCK_READ_SECS", "90")
    monkeypatch.setenv(
        "TUSKER_PROVIDER_ATTEMPT_TIMEOUT_OVERRIDES_JSON",
        '{"local-llm": 240, "mlx-mac": 240, "zai": 90}',
    )

    assert _non_stream_total_secs("local-llm", "qwen3-vl:8b") == 240.0
    assert _non_stream_total_secs("mlx-mac", "ornith-1.5:35b") == 240.0
    # A route with no override keeps the socket floor, never a shorter one.
    assert _non_stream_total_secs("google", "gemini") == 90.0


def test_non_stream_chat_sends_the_configured_budget_to_aiohttp(monkeypatch):
    """The resolved budget must reach aiohttp, not just the helper.

    Guards the wiring: a correct helper is worthless if the request still
    passes a literal 120.
    """
    monkeypatch.setenv("TUSKER_UPSTREAM_SOCK_READ_SECS", "90")
    monkeypatch.setenv(
        "TUSKER_PROVIDER_ATTEMPT_TIMEOUT_OVERRIDES_JSON", '{"local-llm": 240}'
    )

    seen: dict[str, object] = {}

    class _Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

        @property
        def status(self):
            return 200

        def release(self):
            pass

    class _Http:
        def request(self, method, url, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            return _Resp()

    client = PassthroughClient({}, MagicMock(), _Http())

    async def _run():
        await client.chat("local-llm", "qwen3-vl:8b", [{"role": "user", "content": "hi"}])

    asyncio.run(_run())

    timeout = seen["timeout"]
    assert timeout is not None, "no ClientTimeout was passed to the upstream request"
    assert timeout.total == 240.0, f"non-stream total {timeout.total}s pre-empts the budget"
