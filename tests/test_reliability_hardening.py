from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from tusker_gateway.errors import ProviderError
from tusker_gateway.passthrough import PassthroughClient


class _Response:
    status = 200
    url = "https://provider.example/stream"

    def __init__(self) -> None:
        self.release = MagicMock()


@pytest.mark.asyncio
async def test_upstream_disconnect_is_not_converted_to_clean_eof():
    client = object.__new__(PassthroughClient)
    client._record_stream_failure = AsyncMock()
    client._release_capacity = MagicMock()

    async def disconnected():
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise aiohttp.ServerDisconnectedError()

    stream = client._stream_events(
        _Response(),
        provider="test",
        model="model",
        stream_iterator=disconnected(),
    )
    with pytest.raises(aiohttp.ServerDisconnectedError):
        _ = [chunk async for chunk in stream]
    client._record_stream_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_clean_eof_without_terminal_event_is_rejected():
    client = object.__new__(PassthroughClient)
    client._record_stream_failure = AsyncMock()
    client._release_capacity = MagicMock()

    async def incomplete():
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'

    stream = client._stream_events(
        _Response(),
        provider="test",
        model="model",
        stream_iterator=incomplete(),
    )
    with pytest.raises(ProviderError) as exc_info:
        _ = [chunk async for chunk in stream]
    assert exc_info.value.code == "upstream_stream_incomplete"
    client._record_stream_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_quality_store_is_offloaded_and_recorded():
    client = object.__new__(PassthroughClient)
    client._quality = MagicMock()
    client._quality.record = MagicMock()
    await client._record_quality("provider", "model", True, 12.5)
    client._quality.record.assert_called_once_with("provider", "model", True, 12.5)


@pytest.mark.asyncio
async def test_response_failed_terminal_is_recorded_as_failure():
    """A ``response.failed`` terminal event must trigger failure accounting, not success.

    Pre-fix regression: ``_stream_frame_is_terminal`` treated ``response.failed``
    as a successful terminal event, and ``_stream_events`` then ran the success
    branch and called ``_record_quality(success=True)``. This prevented the
    circuit breaker / cooldowns from activating and corrupted the quality
    DB. ``_stream_frame_is_failure`` now detects these events and raises a
    ProviderError, sending the failure through the existing failure path.
    """
    client = object.__new__(PassthroughClient)
    client._record_stream_failure = AsyncMock()
    client._record_quality = AsyncMock()
    client._release_capacity = MagicMock()

    async def failed():
        yield b"data: {\"type\":\"response.failed\",\"error\":{\"code\":\"server_error\"}}\n\n"

    stream = client._stream_events(
        _Response(),
        provider="test",
        model="model",
        stream_iterator=failed(),
    )
    with pytest.raises(ProviderError) as exc_info:
        _ = [chunk async for chunk in stream]
    assert exc_info.value.code == "upstream_stream_invalid"
    client._record_stream_failure.assert_awaited_once()
    # Quality must NOT be recorded as success.
    for call in client._record_quality.await_args_list:
        assert call.args[2] is False, "response.failed must not record success"


@pytest.mark.asyncio
async def test_response_incomplete_terminal_is_recorded_as_failure():
    """A ``response.incomplete`` terminal indicates provider cutoff mid-stream.

    It must take the failure path so the gateway exits cooldown-eligible and
    stops preferring the broken model.
    """
    client = object.__new__(PassthroughClient)
    client._record_stream_failure = AsyncMock()
    client._record_quality = AsyncMock()
    client._release_capacity = MagicMock()

    async def incomplete():
        yield b"data: {\"type\":\"response.incomplete\",\"reason\":\"max_output_tokens\"}\n\n"

    stream = client._stream_events(
        _Response(),
        provider="test",
        model="model",
        stream_iterator=incomplete(),
    )
    with pytest.raises(ProviderError) as exc_info:
        _ = [chunk async for chunk in stream]
    assert exc_info.value.code == "upstream_stream_invalid"
    client._record_stream_failure.assert_awaited_once()
    for call in client._record_quality.await_args_list:
        assert call.args[2] is False, "response.incomplete must not record success"
