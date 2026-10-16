"""Regression scenarios for the historical stream/provider fault classes.

Each test below targets a real failure mode that the gateway has seen on
production traffic: mid-stream close, transient 502, request-level 413,
repeated-reasoning cycles, and shape-reality replay of a real OMP turn.
They use the same ``client`` fixture as the rest of the chat-completions
tests, so any drift in the HTTP surface will be caught here too.

The bounded per-attempt idle budget (a candidate that connects but never
yields) is already exercised at the unit level in
``test_pool_fallback_bounded_per_attempt_timeout`` in ``test_chat.py``;
running the same scenario at the HTTP boundary duplicates coverage
without catching anything new because the test fixture's empty-byte
chunks count as progress.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tusker_gateway import cooldown as cooldown_module
from tusker_gateway.errors import ProviderError

from .conftest import HEADERS_AUTH

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "hermes_code_turn.json"


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _ok_content_frames(text: str) -> list[bytes]:
    return [
        _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}),
        _sse({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n\n",
    ]


# ---------------------------------------------------------------------------
# Mid-stream close before finish_reason
# ---------------------------------------------------------------------------


async def test_mid_stream_close_surfaces_partial_content(client):
    """A provider that yields one frame then drops the TCP connection must
    at least surface the partial content the client already received and
    not pretend the stream finished cleanly.

    This is the production shape of the 2026-10-14 00:17Z socket-closed turn
    from the tusker-ai-gateway project (300.4s client timeout). The gateway
    cannot fall back once visible content has been committed: it logs the
    upstream failure and lets the body end without a clean [DONE] so the
    client can rely on its own read timeout to verdict.
    """
    async def half_stream(*args, **kwargs):
        yield _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        yield _sse({"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]})
        raise ConnectionResetError("peer closed without [DONE]")

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
        return_value=half_stream(),
    ):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        body = await response.read()

    assert response.status == 200
    # The partial content reaches the client.
    assert b"partial" in body
    # vLLM/OpenRouter shape: a top-level `error` object so the OpenAI SDK
    # raises APIError instead of absorbing the failure into a successful
    # assistant turn. The 2026-10-14 00:17Z 300.4s timeout case is fixed
    # by this — OMP now gets a verdict instead of waiting on its read
    # timeout.
    assert b'"error":' in body
    assert b'"code": "stream_closed_prematurely"' in body
    # The stream is closed cleanly with [DONE] after the error frame so
    # any eventsource-parser client (OpenAI SDK, OMP, verifiers, etc.)
    # sees a proper stream end.
    assert b"data: [DONE]" in body


# ---------------------------------------------------------------------------
# Provider returns 502: cooldown + failover to next candidate
# ---------------------------------------------------------------------------


async def test_provider_502_falls_over_to_next_candidate(app, client):
    """A 502 from the first candidate must be a transient-class failure:
    the gateway tries the next candidate, and the client sees a
    successful SSE stream from the fallback.
    """
    pool_manager = MagicMock()
    pool_manager.fallback_pools.return_value = ()
    pool_manager.select.side_effect = [
        ("alibaba", "transient-502"),
        ("openrouter", "healthy-model"),
    ]
    app["pool_manager"] = pool_manager

    failure = ProviderError("upstream chat failed")
    failure.upstream_status = 502
    failure.upstream_body = "<html><body>502 Bad Gateway</body></html>"

    async def healthy_stream(*args, **kwargs):
        for chunk in _ok_content_frames("recovered after 502"):
            yield chunk

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        mock_chat.side_effect = [failure, healthy_stream()]
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        body = await response.read()

    assert response.status == 200
    assert b"recovered after 502" in body
    assert pool_manager.select.call_count == 2


# ---------------------------------------------------------------------------
# 413 is request-level: the route is not poisoned
# ---------------------------------------------------------------------------


async def test_request_level_413_does_not_poison_the_route(app, client):
    """A 413 from a provider is a property of the request, not the route.
    It must NOT apply a per-route cooldown: a smaller prompt from a
    different caller should still be tried on the same (provider, model).
    The client sees a sanitized 502 (or forward of 413) so they can
    reduce the prompt instead of retrying into the same wall.
    """
    pool_manager = MagicMock()
    pool_manager.fallback_pools.return_value = ()
    pool_manager.select.side_effect = [
        ("alibaba", "fits-no-windows"),
        ("openrouter", "also-fits-no-windows"),
    ]
    app["pool_manager"] = pool_manager

    failure = ProviderError("prompt too large")
    failure.upstream_status = 413
    failure.upstream_body = '{"error":{"message":"too many tokens"}}'

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
        side_effect=[failure, failure],
    ):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "x" * 800_000}],
                "stream": False,
            },
            headers=HEADERS_AUTH,
        )

    # Provider failures are sanitized to 502 (or 413 forwarded); the
    # caller can use either to decide the prompt is too large.
    assert response.status in (400, 413, 502)
    # The (provider, model) tuple must NOT have a per-route cooldown —
    # a smaller prompt from a different caller should still try it.
    models = cooldown_module.global_tracker().snapshot()["models"]
    cooled = {(entry["provider"], entry["model"]) for entry in models}
    assert ("alibaba", "fits-no-windows") not in cooled


# ---------------------------------------------------------------------------
# Cycle capture: the loop detector + capture file integration
# ---------------------------------------------------------------------------


async def test_cycle_detector_writes_capture_file_end_to_end(
    app, client, tmp_path, monkeypatch
):
    """The cycle detector must classify a repeated reasoning stream as a
    provider_stream_loop, fail over to the next candidate, and write the
    captured cycle to the configured diagnostics directory.
    """
    monkeypatch.setenv("TUSKER_STREAM_DIAGNOSTICS_DIR", str(tmp_path / "cycles"))
    pool_manager = MagicMock()
    pool_manager.fallback_pools.return_value = ()
    pool_manager.select.side_effect = [
        ("alibaba", "looping-model"),
        ("openrouter", "healthy-model"),
    ]
    app["pool_manager"] = pool_manager

    # 48-char cycle, 8 repeats → 384 chars total, well past the 256-char
    # trigger threshold and within the 32–1024 cycle length range.
    cycle = "inspect, reason, and continue with the task now. "  # 48 chars

    async def loop_stream(*args, **kwargs):
        for _ in range(8):
            yield _sse({"choices": [{"index": 0, "delta": {"reasoning_content": cycle}, "finish_reason": None}]})

    async def healthy_stream(*args, **kwargs):
        for chunk in _ok_content_frames("recovered from cycle"):
            yield chunk

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        mock_chat.side_effect = [loop_stream(), healthy_stream()]
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{
                    "type": "function",
                    "function": {"name": "bash", "parameters": {"type": "object"}},
                }],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        body = await response.read()

    assert response.status == 200
    assert b"recovered from cycle" in body
    # The cycle capture directory must contain one cycle-*.jsonl file.
    captures = list((tmp_path / "cycles").glob("cycle-*.jsonl"))
    assert len(captures) == 1, "expected exactly one cycle capture file"
    record = json.loads(captures[0].read_text())
    assert record["provider"] == "alibaba"
    assert record["model"] == "looping-model"
    assert record["cycle_chars"] >= 32
    assert record["repeats"] >= 3
    assert "inspect" in record["text"]


# ---------------------------------------------------------------------------
# Real-turn shape replay: an OMP-shaped hermes-code turn replays cleanly
# ---------------------------------------------------------------------------


async def test_real_hermes_code_turn_replays_cleanly(client, monkeypatch):
    """The hermes-code turn captured from a live OMP session (12 tools,
    system + user) must round-trip through the gateway without
    qualification or schema errors. This guards against drift in the
    OpenAI-compatible shape that the gateway accepts: if the request body
    is rejected at the boundary the test fails before the stream even
    starts.
    """
    fixture = json.loads(FIXTURE_PATH.read_text())

    async def healthy_stream(*args, **kwargs):
        for chunk in _ok_content_frames("replayed turn succeeded"):
            yield chunk

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
        return_value=healthy_stream(),
    ):
        response = await client.post(
            "/v1/chat/completions",
            json=fixture,
            headers=HEADERS_AUTH,
        )
        body = await response.read()

    assert response.status == 200
    assert b"replayed turn succeeded" in body
    assert b"data: [DONE]" in body



async def test_provider_stream_closed_error_is_not_classified_as_client_disconnect():
    """The new ProviderStreamClosedError must reach the vLLM-shape error
    chunk path, not the silent "client_disconnected" teardown. If the two
    exception classes were ever conflated, the OMP-visible 300s timeout
    case would reappear.
    """
    from tusker_gateway.errors import ProviderStreamClosedError, ProviderError

    exc = ProviderStreamClosedError(
        provider="alibaba", model="qwen3.x-flash", detail="ConnectionResetError"
    )
    assert isinstance(exc, ProviderError)
    # Crucial: must NOT be a ConnectionError, so the outer catch at
    # endpoints.py:7010 (`except (ConnectionResetError, ...)`) does not
    # silently treat an upstream failure as a client disconnect.
    assert not isinstance(exc, ConnectionResetError)
    assert not isinstance(exc, ConnectionError)
    assert not isinstance(exc, BrokenPipeError)
    assert exc.code == "stream_closed_prematurely"
