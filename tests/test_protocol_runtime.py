"""Runtime evidence opt-in and endpoint isolation."""
from tusker_gateway.protocol_runtime import (
    endpoint_identity, verified_features_for_endpoint,
)


def test_endpoint_identity_ignores_credentials():
    first = {"base_url": "https://provider.example/v1",
             "chat_path": "/chat/completions", "api_key": "secret-one"}
    second = {**first, "api_key": "secret-two"}
    assert endpoint_identity(first) == endpoint_identity(second)
    assert endpoint_identity(first) != endpoint_identity({
        **first, "base_url": "https://other.example/v1",
    })


def test_evidence_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TUSKER_PROTOCOL_EVIDENCE_ENABLED", raising=False)
    assert verified_features_for_endpoint("deepseek", "deepseek-chat", {
        "base_url": "https://provider.example/v1",
    }) == frozenset()
