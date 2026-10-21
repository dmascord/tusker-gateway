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

import aiohttp
import pytest

from tusker_gateway import cooldown as cooldown_module
from tusker_gateway.errors import ProviderError, RateLimitError

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
    _candidates = iter([
        ("alibaba", "fits-no-windows"),
        ("openrouter", "also-fits-no-windows"),
    ])
    pool_manager.select.side_effect = lambda *_a, **_k: next(_candidates, None)
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


async def test_truncated_sse_body_surfaces_stream_closed_envelope(client):
    """A truncated upstream SSE body must reach the vLLM/OpenRouter-shape
    ``error`` + ``[DONE]`` envelope rather than the generic provider-error
    text frame.

    aiohttp raises ``ClientPayloadError`` (not ``ConnectionResetError``) for
    a body that ends before its chunked terminator — verified for both a TCP
    abort and an RST against aiohttp 3.13.3. Before it was added to the
    wrap set at ``endpoints.diagnostic_raw_stream``, the documented
    ``stream_closed_prematurely`` envelope was unreachable for the exact
    production shape it exists to cover (AGENTS.md: 2026-10-14 00:17Z).
    """
    async def truncated_stream(*args, **kwargs):
        yield _sse({"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]})
        raise aiohttp.ClientPayloadError(
            "Response payload is not completed: <TransferEncodingError: 400, "
            "message='Not enough data to satisfy transfer length header.'>"
        )

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
        return_value=truncated_stream(),
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
    # Partial content still reaches the client.
    assert b"partial" in body
    # The OpenAI SDK raises APIError on the top-level error object.
    assert b'"error":' in body
    assert b'"code": "stream_closed_prematurely"' in body
    assert b'"type": "upstream_stream_error"' in body
    assert b"data: [DONE]" in body


async def test_truncated_sse_body_fails_over_before_visible_output(app, client):
    """A truncated body with no assistant content yet must still try one
    alternative candidate.

    Wrapping the aiohttp exception as ``ProviderStreamClosedError`` removed
    it from the ``ClientError`` branch of
    ``_stream_failure_can_fail_over``; ``stream_closed_prematurely`` must
    therefore be a declared recovery code or the pre-visible failover is
    silently lost.
    """
    pool_manager = MagicMock()
    pool_manager.select.side_effect = [
        ("alpha", "truncating-model"),
        ("beta", "healthy-model"),
    ]
    pool_manager.fallback_pools.return_value = ()
    app["pool_manager"] = pool_manager

    async def truncating_stream(*args, **kwargs):
        yield _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        raise aiohttp.ClientPayloadError("Response payload is not completed")

    async def healthy_stream(*args, **kwargs):
        for frame in _ok_content_frames("recovered output"):
            yield frame
        yield b"data: [DONE]\n\n"

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        mock_chat.side_effect = [truncating_stream(), healthy_stream()]
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
    assert b"recovered output" in body
    assert b"stream_closed_prematurely" not in body
    assert pool_manager.select.call_count == 2
    assert pool_manager.select.call_args_list[1].kwargs["excluded"] == {
        ("alpha", "truncating-model")
    }


async def test_midstream_rate_limit_fails_over_before_visible_output(app, client):
    """A 429 that arrives mid-commit must try one alternative candidate.

    ``rate_limit_exceeded`` was absent from ``_STREAM_RECOVERY_CODES``, so the
    pre-visible failover gate refused it and the generic
    "Upstream provider rate limit exceeded; retry shortly." frame became the
    client's terminal answer (2026-10-21) even though healthy candidates were
    still eligible. The 429 carries no assistant-visible output, so it belongs
    on the same single pre-visible failover as a truncated body.
    """
    pool_manager = MagicMock()
    pool_manager.select.side_effect = [
        ("alpha", "throttled-model"),
        ("beta", "healthy-model"),
    ]
    pool_manager.fallback_pools.return_value = ()
    app["pool_manager"] = pool_manager

    async def throttled_stream(*args, **kwargs):
        yield _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        raise RateLimitError(
            "Upstream 429",
            code="rate_limit_exceeded",
            body='{"statusCode":429,"message":"Token limit is exceeded. Try again in 8 seconds."}',
            headers={"Retry-After": "8"},
        )

    async def healthy_stream(*args, **kwargs):
        for frame in _ok_content_frames("recovered output"):
            yield frame
        yield b"data: [DONE]\n\n"

    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        mock_chat.side_effect = [throttled_stream(), healthy_stream()]
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
    assert b"recovered output" in body
    assert b"rate limit exceeded" not in body
    assert pool_manager.select.call_count == 2
    assert pool_manager.select.call_args_list[1].kwargs["excluded"] == {
        ("alpha", "throttled-model")
    }
