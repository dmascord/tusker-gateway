"""Regression coverage for commandcode provider routing and headers.

Enforcement of privacy-only \"x-cmd-zdr: 1\" and bearer authentication via
COMMAND_CODE_API_KEY.
"""

from __future__ import annotations

import os
import pytest

from unittest.mock import MagicMock, patch
from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY, load_config
from tusker_gateway.passthrough import PassthroughClient
from tusker_gateway.quality import QualityDB


def test_commandcode_provider_registered():
    """Command Code provider must be in the DEFAULT_PROVIDER_REGISTRY."""
    assert "commandcode" in DEFAULT_PROVIDER_REGISTRY
    pc = DEFAULT_PROVIDER_REGISTRY["commandcode"]
    assert pc.name == "commandcode"
    assert pc.kind == "bearer"
    assert pc.base_url == "https://api.commandcode.ai/provider/v1"
    assert pc.chat_path == "/chat/completions"
    assert pc.auth_env == "COMMAND_CODE_API_KEY"
    assert pc.models_path == "/models"
    assert pc.zdr_ok is True


def test_commandcode_key_loading():
    """COMMAND_CODE_API_KEY env var is loaded into provider_api_keys."""
    env = {
        "API_KEYS": "k1",
        "COMMAND_CODE_API_KEY": "cmd-private-key",
    }
    clear_keys = [k for k in os.environ if k.startswith("PROVIDER_")]
    with patch.dict(os.environ, env, clear=False):
        for k in clear_keys:
            os.environ.pop(k, None)
        cfg = load_config()
    assert cfg["provider_api_keys"]["commandcode"] == "cmd-private-key"


@pytest.mark.asyncio
async def test_commandcode_privacy_pool_uses_zdr_header():
    """Command Code in the privacy pool must send \"x-cmd-zdr: 1\"."""
    config = {
        "api_keys": ["gw-test-key"],
        "provider_api_keys": {"commandcode": "cmd-privacy-key"},
        "codex_credentials": [],
        "quality_db_path": ":memory:",
    }
    config["providers"] = {"commandcode": DEFAULT_PROVIDER_REGISTRY["commandcode"]}

    client = PassthroughClient(config, QualityDB(":memory:"), MagicMock())
    endpoint = {
        "base_url": "https://api.commandcode.ai/provider/v1",
        "chat_path": "/chat/completions",
        "auth_type": "bearer",
    }

    headers, body = await client._build_request(
        "commandcode",
        "deepseek/deepseek-v4-flash",
        [{"role": "user", "content": "hello"}],
        stream=False,
        api_key=None,
        extra_headers=None,
        extra_body=None,
        endpoint=endpoint,
        pool="privacy",  # privacy-pool context
    )

    assert headers["Authorization"] == "Bearer cmd-privacy-key"
    assert headers["x-cmd-zdr"] == "1"
    assert body["model"] == "deepseek/deepseek-v4-flash"


@pytest.mark.asyncio
async def test_commandcode_code_pool_does_not_use_zdr_header():
    """Command Code in the code pool must NOT send \"x-cmd-zdr\"."""
    config = {
        "api_keys": ["gw-test-key"],
        "provider_api_keys": {"commandcode": "cmd-code-key"},
        "codex_credentials": [],
        "quality_db_path": ":memory:",
    }
    config["providers"] = {"commandcode": DEFAULT_PROVIDER_REGISTRY["commandcode"]}

    client = PassthroughClient(config, QualityDB(":memory:"), MagicMock())
    endpoint = {
        "base_url": "https://api.commandcode.ai/provider/v1",
        "chat_path": "/chat/completions",
        "auth_type": "bearer",
    }

    headers, body = await client._build_request(
        "commandcode",
        "deepseek/deepseek-v4-flash",
        [{"role": "user", "content": "hello"}],
        stream=False,
        api_key=None,
        extra_headers=None,
        extra_body=None,
        endpoint=endpoint,
        pool="code",  # code-pool context
    )

    assert headers["Authorization"] == "Bearer cmd-code-key"
    assert headers.get("x-cmd-zdr") is None
    assert body["model"] == "deepseek/deepseek-v4-flash"


@pytest.mark.asyncio
async def test_commandcode_missing_key_uses_provider_api_keys():
    """If api_key is None, Authorization comes from provider_api_keys."""
    config = {
        "api_keys": ["gw-test-key"],
        "provider_api_keys": {
            "commandcode": "cmd-default-key",
            "openai": "sk-oa",
        },
        "codex_credentials": [],
        "quality_db_path": ":memory:",
    }
    config["providers"] = {"commandcode": DEFAULT_PROVIDER_REGISTRY["commandcode"]}

    client = PassthroughClient(config, QualityDB(":memory:"), MagicMock())
    endpoint = {
        "base_url": "https://api.commandcode.ai/provider/v1",
        "chat_path": "/chat/completions",
        "auth_type": "bearer",
    }

    headers, body = await client._build_request(
        "commandcode",
        "deepseek/deepseek-v4-flash",
        [{"role": "user", "content": "hello"}],
        stream=False,
        api_key=None,  # Explicitly None
        extra_headers=None,
        extra_body=None,
        endpoint=endpoint,
        pool="privacy",
    )

    assert headers["Authorization"] == "Bearer cmd-default-key"
    assert headers["x-cmd-zdr"] == "1"
