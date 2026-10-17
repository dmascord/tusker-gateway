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
