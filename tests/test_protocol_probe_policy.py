"""Configured endpoint probe policy fixtures."""
import pytest

from tusker_gateway.protocol_probe_policy import authorize_probe_target


def test_configured_endpoint_can_be_approved():
    endpoint = {"base_url": "https://upstream.example/v1",
                "chat_path": "/chat/completions"}
    result = authorize_probe_target(
        provider="deepseek", model="deepseek-chat", endpoint=endpoint,
        approved=True, configured_endpoints=[endpoint])
    assert result.provider == "deepseek"
    assert result.endpoint_id


def test_unconfigured_endpoint_is_rejected():
    endpoint = {"base_url": "https://other.example/v1",
                "chat_path": "/chat/completions"}
    with pytest.raises(PermissionError):
        authorize_probe_target(
            provider="deepseek", model="deepseek-chat", endpoint=endpoint,
            approved=True, configured_endpoints=[{
                "base_url": "https://upstream.example/v1",
                "chat_path": "/chat/completions",
            }])


def test_operator_approval_is_required():
    endpoint = {"base_url": "https://upstream.example/v1"}
    with pytest.raises(PermissionError):
        authorize_probe_target(
            provider="deepseek", model="deepseek-chat", endpoint=endpoint,
            approved=False, configured_endpoints=[endpoint])
