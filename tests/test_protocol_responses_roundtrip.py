"""Responses API roundtrip positive and negative fixtures."""
import pytest

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_responses_roundtrip import probe_responses_roundtrip


@pytest.mark.asyncio
async def test_responses_roundtrip_with_matching_call_id(tmp_path):
    calls = []
    async def execute(request):
        calls.append(request)
        if len(calls) == 1:
            return {"status": "completed", "output": [{
                "type": "function_call", "name": "protocol_probe_echo",
                "call_id": "call_1", "arguments": '{"value":"ok"}',
            }]}
        assert request["input"][-1] == {
            "type": "function_call_output", "call_id": "call_1",
            "output": '{"value":"ok"}',
        }
        return {"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": "ok"}],
        }]}
    store = ProtocolEvidenceStore(str(tmp_path / "evidence.db"))
    result = await probe_responses_roundtrip(
        provider="openai", endpoint_id="native", model="test",
        execute=execute, store=store)
    assert result.status == "passed"
    assert len(calls) == 2
    assert store.get("openai", "native", "test", "responses_function_calls").verified


@pytest.mark.asyncio
async def test_responses_roundtrip_no_call_is_unknown(tmp_path):
    calls = []
    async def execute(request):
        calls.append(request)
        return {"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": "hello"}],
        }]}
    store = ProtocolEvidenceStore(str(tmp_path / "evidence.db"))
    result = await probe_responses_roundtrip(
        provider="proxy", endpoint_id="hosted", model="test",
        execute=execute, store=store)
    assert result.status == "unknown"
    assert len(calls) == 1
