"""SSE tool-fragment probe tests."""
import json

import pytest

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_stream_probe import probe_streaming_tool_fragments


@pytest.mark.asyncio
async def test_streamed_tool_call_fragments_are_assembled(tmp_path):
    def frame(delta, finish=None):
        return ("data: " + json.dumps({"choices": [{
            "index": 0, "delta": delta, "finish_reason": finish,
        }]}) + "\n\n").encode()

    async def execute(request):
        async def chunks():
            yield frame({"tool_calls": [{"index": 0, "id": "call_1",
                "function": {"name": "protocol_probe_echo", "arguments": '{"value":'}}]})
            yield frame({"tool_calls": [{"index": 0,
                "function": {"arguments": '"ok"}'}}]})
            yield frame({}, "tool_calls")
            yield b"data: [DONE]\n\n"
        return chunks()

    store = ProtocolEvidenceStore(str(tmp_path / "evidence.db"))
    result = await probe_streaming_tool_fragments(
        provider="deepseek", endpoint_id="native", model="deepseek-chat",
        execute=execute, store=store)
    assert result.status == "passed"
    assert store.get("deepseek", "native", "deepseek-chat", "streaming_tool_fragments").verified
