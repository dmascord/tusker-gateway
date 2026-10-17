"""Two-turn reasoning replay probe tests."""
import pytest

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_replay_probe import probe_reasoning_replay


@pytest.mark.asyncio
async def test_replay_requires_real_second_turn(tmp_path):
    requests = []
    async def execute(body):
        requests.append(body)
        if len(requests) == 1:
            return {"choices": [{"message": {
                "role": "assistant", "content": None,
                "reasoning_content": "I should use the tool",
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "protocol_probe_echo",
                                             "arguments": '{"value":"ok"}'}}],
            }}]}
        assert body["messages"][1]["reasoning_content"] == "I should use the tool"
        assert body["messages"][2]["tool_call_id"] == "call_1"
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    store = ProtocolEvidenceStore(str(tmp_path / "evidence.db"))
    result = await probe_reasoning_replay(
        provider="deepseek", endpoint_id="native", model="deepseek-chat",
        execute=execute, store=store)
    assert result.status == "passed"
    assert len(requests) == 2
    assert store.get("deepseek", "native", "deepseek-chat", "reasoning_replay").verified


@pytest.mark.asyncio
async def test_no_reasoning_is_inconclusive(tmp_path):
    calls = []
    async def execute(body):
        calls.append(body)
        return {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
    store = ProtocolEvidenceStore(str(tmp_path / "evidence.db"))
    result = await probe_reasoning_replay(
        provider="openrouter", endpoint_id="hosted", model="deepseek-r1",
        execute=execute, store=store)
    assert result.status == "unknown"
    assert len(calls) == 1
