"""Protocol evidence tests; deliberately not executed during authoring."""
import time

import pytest

from tusker_gateway.protocol_evidence import (
    ProtocolEvidenceStore, PROTOCOL_PROBE_VERSION, MAX_EVIDENCE_AGE_SECONDS,
)
from tusker_gateway.protocol_compat import resolve_with_evidence


def test_endpoint_scoped_protocol_evidence(tmp_path):
    store = ProtocolEvidenceStore(str(tmp_path / "protocol.db"))
    store.record("openrouter", "endpoint-a", "deepseek-r1",
                 "reasoning_channel", "passed")
    profile, features = resolve_with_evidence(
        "openrouter", "deepseek-r1", "endpoint-a", store=store)
    assert profile.family == "deepseek"
    assert "reasoning_channel" in features
    _, other = resolve_with_evidence(
        "openrouter", "deepseek-r1", "endpoint-b", store=store)
    assert "reasoning_channel" not in other


def test_stale_evidence_is_not_verified(tmp_path):
    store = ProtocolEvidenceStore(str(tmp_path / "protocol.db"))
    store.record("zai", "zai-native", "glm-4.7",
                 "reasoning_replay", "passed",
                 checked_at=time.time() - MAX_EVIDENCE_AGE_SECONDS - 10)
    _, features = resolve_with_evidence(
        "zai", "glm-4.7", "zai-native", store=store)
    assert "reasoning_replay" not in features


def test_unavailable_does_not_imply_unsupported(tmp_path):
    store = ProtocolEvidenceStore(str(tmp_path / "protocol.db"))
    store.record("deepseek", "native", "deepseek-chat",
                 "structured_tool_calls", "unavailable")
    item = store.get("deepseek", "native", "deepseek-chat", "structured_tool_calls")
    assert item is not None and item.status == "unavailable"
    assert not item.verified


def test_unknown_feature_is_rejected(tmp_path):
    store = ProtocolEvidenceStore(str(tmp_path / "protocol.db"))
    with pytest.raises(ValueError):
        store.record("provider", "endpoint", "model", "arbitrary", "passed")
