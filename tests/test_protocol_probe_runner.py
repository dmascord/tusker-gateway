"""Opt-in probe runner contract tests."""
import asyncio

import pytest

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_probe_runner import probe_chat_contract


@pytest.mark.asyncio
async def test_probe_runs_once_and_persists_only_evidence(tmp_path):
    calls = []
    async def execute(request):
        calls.append(request)
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "protocol_probe_echo",
                                         "arguments": '{"value":"ok"}'}}]}}]}
    store = ProtocolEvidenceStore(str(tmp_path / "protocol.db"))
    results = await probe_chat_contract(
        provider="native", endpoint_id="endpoint-1", model="test",
        execute=execute, store=store)
    assert len(calls) == 1
    assert results["structured_tool_calls"].status == "passed"
    assert results["tool_call_finish"].status == "passed"
    assert results["reasoning_channel"].status == "unknown"
    assert store.get("native", "endpoint-1", "test", "structured_tool_calls").verified


@pytest.mark.asyncio
async def test_timeout_does_not_mark_unsupported(tmp_path):
    async def execute(request):
        await asyncio.sleep(1)
        return {}
    store = ProtocolEvidenceStore(str(tmp_path / "protocol.db"))
    result = await probe_chat_contract(
        provider="native", endpoint_id="endpoint-1", model="test",
        execute=execute, store=store, timeout_seconds=0.001)
    assert result["structured_tool_calls"].status == "unavailable"
    assert not store.get("native", "endpoint-1", "test", "structured_tool_calls").verified


@pytest.mark.asyncio
async def test_cancellation_propagates_and_records_nothing(tmp_path):
    """Operator cancellation must not be laundered into probe evidence.

    ``asyncio.CancelledError`` inherits from ``BaseException``, so the
    ``except (TimeoutError, ConnectionError, OSError)`` clause must not
    absorb it. Recording it as ``unavailable`` would silently invert the
    operator's intent, and swallowing it would leave the task un-cancelled
    for the caller's shutdown path.
    """
    started = asyncio.Event()

    async def execute(request):
        started.set()
        await asyncio.sleep(30)
        return {}

    store = ProtocolEvidenceStore(str(tmp_path / "protocol.db"))
    task = asyncio.create_task(probe_chat_contract(
        provider="native", endpoint_id="endpoint-1", model="test",
        execute=execute, store=store))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get("native", "endpoint-1", "test", "structured_tool_calls") is None


@pytest.mark.asyncio
async def test_stream_probe_cancellation_propagates(tmp_path):
    from tusker_gateway.protocol_stream_probe import probe_streaming_tool_fragments

    started = asyncio.Event()

    async def execute(request):
        async def chunks():
            started.set()
            await asyncio.sleep(30)
            yield b"data: [DONE]\n\n"
        return chunks()

    store = ProtocolEvidenceStore(str(tmp_path / "protocol.db"))
    task = asyncio.create_task(probe_streaming_tool_fragments(
        provider="native", endpoint_id="endpoint-1", model="test",
        execute=execute, store=store))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get(
        "native", "endpoint-1", "test", "streaming_tool_fragments") is None
