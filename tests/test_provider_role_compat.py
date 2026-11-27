"""Regression coverage for provider role compatibility on local backends.

Ollama's Go chat templates accept only the classic roles. A turn that
carries OpenAI's newer ``developer`` role makes the template raise
("System message must be at the beginning of the conversation"), and the
MLX Mac answered HTTP 500 on 2026-11-27 while the same content sent as
``system`` returned 200. The gateway folds the role before dispatch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tusker_gateway.passthrough import (
    _fold_developer_role,
    PassthroughClient,
)
from tusker_gateway.quality import QualityDB


def _client(**overrides) -> PassthroughClient:
    config = {
        "api_keys": ["gw-test-key"],
        "provider_api_keys": {"mlx-mac": "mlx-key", "openai": "openai-key"},
        "providers": {
            "mlx-mac": {
                "kind": "local",
                "base_url": "http://10.0.0.141:11434",
                "chat_path": "/v1/chat/completions",
                "zdr_ok": True,
            },
            "openai": {
                "kind": "bearer",
                "base_url": "https://api.openai.com/v1",
                "chat_path": "/chat/completions",
            },
        },
        "quality_db_path": ":memory:",
    }
    config.update(overrides)
    return PassthroughClient(config, QualityDB(":memory:"), MagicMock())


@pytest.mark.parametrize("provider", ["mlx-mac", "local-llm"])
def test_developer_role_is_folded_to_system_for_local_providers(provider):
    messages = [
        {"role": "developer", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    folded = _fold_developer_role(messages, provider)
    assert [m["role"] for m in folded] == ["system", "user"]
    assert folded[0]["content"] == "be terse"


@pytest.mark.parametrize("provider", ["openai", "openai-codex", "google", None])
def test_developer_role_is_untouched_for_other_providers(provider):
    messages = [
        {"role": "developer", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    assert _fold_developer_role(messages, provider) == messages


def test_folding_preserves_the_other_message_fields():
    message = {
        "role": "developer",
        "content": "be terse",
        "name": "policy",
        "tool_call_id": "abc",
    }
    folded = _fold_developer_role([message], "mlx-mac")
    assert folded[0]["role"] == "system"
    assert folded[0]["name"] == "policy"
    assert folded[0]["tool_call_id"] == "abc"


def test_folding_is_case_insensitive_on_the_role():
    folded = _fold_developer_role([{"role": "Developer", "content": "x"}], "mlx-mac")
    assert folded[0]["role"] == "system"


def test_folding_leaves_non_list_input_alone():
    assert _fold_developer_role("not a list", "mlx-mac") == "not a list"


@pytest.mark.asyncio
async def test_upstream_request_uses_system_role_for_mlx_mac():
    """The dispatched body must not carry a developer role to Ollama."""
    client = _client()
    captured: dict = {}

    class _Resp:
        status = 200

        async def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        async def text(self):
            return ""

        @property
        def headers(self):
            return {}

    class _Session:
        def request(self, method, url, **kwargs):
            captured["body"] = kwargs.get("json")
            captured["url"] = url
            return _Ctx()

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *exc):
            return False

    client._http = _Session()

    def _request(method, url, **kwargs):
        captured["body"] = kwargs.get("json")
        captured["url"] = url
        return _Ctx()

    client._http.request = _request

    await client.chat(
        "mlx-mac",
        "local-model",
        [
            {"role": "developer", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
    )
    body = captured["body"]

def test_developer_after_user_is_reordered_before_system():
    """developer folded to system after user must be moved before user."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "developer", "content": "be terse"},
    ]
    folded = _fold_developer_role(messages, "mlx-mac")
    assert [m["role"] for m in folded] == ["system", "user"]
    assert folded[0]["content"] == "be terse"
    assert folded[1]["content"] == "hi"

def test_multiple_systems_are_merged_into_one_leading_system():
    """Multiple system messages merge into a single leading system message."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "second system"},
        {"role": "system", "content": "first system"},
        {"role": "user", "content": "again"},
    ]
    folded = _fold_developer_role(messages, "mlx-mac")
    assert [m["role"] for m in folded] == ["system", "user", "user"]
    assert folded[0]["content"] == "second system\nfirst system"
    assert folded[1]["content"] == "hi"
    assert folded[2]["content"] == "again"


def test_developer_and_system_are_merged_into_one_leading_system():
    """developer folded to system merges with existing system messages."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "developer", "content": "be terse"},
    ]
    folded = _fold_developer_role(messages, "mlx-mac")
    assert [m["role"] for m in folded] == ["system", "user"]
    assert folded[0]["content"] == "sys\nbe terse"
    assert folded[1]["content"] == "hi"


def test_developer_is_not_reordered_for_other_providers():
    """Non-system-only providers get no reordering or folding."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "developer", "content": "be terse"},
    ]
    assert _fold_developer_role(messages, "openai") == messages
