"""Tests for provider/model safety deny-listing."""
from __future__ import annotations

import os

import pytest

from tusker_gateway.config import (
    PoolConfig,
    load_config,
    model_is_blacklisted,
    tools_include_high_impact,
)
from tusker_gateway.endpoints import (
    _enforce_high_impact_approval,
    _explicit_high_impact_authorization,
    _high_impact_call_kind,
    _validate_complete_tool_response,
)
from tusker_gateway.errors import HighImpactApprovalRequiredError
from tusker_gateway.safety import (
    is_runtime_blacklisted,
    record_suspicious_behavior,
    reset_reputation,
)
from tusker_gateway.pools import PoolManager


def test_builtin_blacklist_blocks_xiaomi_mimo():
    config = {
        "blacklisted_models": ("xiaomi/mimo-v2.5",),
    }
    assert model_is_blacklisted(config, "xiaomi", "mimo-v2.5")
    assert not model_is_blacklisted(config, "openrouter", "mimo-v2.5")


def test_configured_blacklist_supports_provider_model_globs(monkeypatch):
    monkeypatch.setenv("TUSKER_BLACKLISTED_MODELS", "openrouter/*:free, ZAI/GLM-5.3-FLASH")
    config = load_config()
    assert "xiaomi/mimo-v2.5" in config["greylisted_models"]
    assert model_is_blacklisted(config, "openrouter", "cohere/north-mini-code:free")
    assert model_is_blacklisted(config, "zai", "glm-5.3-flash")


def test_blacklisted_model_is_not_selected(tmp_path):
    manager = PoolManager(
        {
            "pools": {
                "code": PoolConfig(
                    "code",
                    [
                        {"provider": "xiaomi", "model": "mimo-v2.5"},
                        {"provider": "openrouter", "model": "safe-model"},
                    ],
                ),
            },
            "blacklisted_models": ("xiaomi/mimo-v2.5",),
            "quality_db_path": os.path.join(tmp_path, "quality.db"),
            "tool_capability_db_path": os.path.join(tmp_path, "capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {"xiaomi": "test", "openrouter": "test"},
        }
    )
    assert manager.select("code") == ("openrouter", "safe-model")


def test_high_impact_tools_only_select_trusted_models(tmp_path):
    manager = PoolManager(
        {
            "pools": {
                "code": PoolConfig(
                    "code",
                    [
                        {"provider": "openrouter", "model": "safe-model"},
                        {"provider": "opencode-go", "model": "trusted-model"},
                    ],
                ),
            },
            "blacklisted_models": (),
            "trusted_action_models": ("opencode-go/*",),
            "quality_db_path": os.path.join(tmp_path, "quality.db"),
            "tool_capability_db_path": os.path.join(tmp_path, "capability.db"),
            "excluded_providers": [],
            "provider_api_keys": {
                "openrouter": "test",
                "opencode-go": "test",
            },
        }
    )
    tools = [{"type": "function", "function": {"name": "place_trade"}}]
    assert tools_include_high_impact(tools)
    assert manager.select("code", high_impact_tools=True) == (
        "opencode-go",
        "trusted-model",
    )


def test_login_debugging_browser_call_is_not_high_impact():
    call = {"function": {"name": "browser", "arguments": '{"action":"click","text":"Log in"}'}}
    assert _high_impact_call_kind(call) is None


def test_trade_task_is_high_impact():
    call = {
        "function": {
            "name": "task",
            "arguments": '{"task":"Place the buy order for 509 units"}',
        },
    }
    assert _high_impact_call_kind(call) == "task"


def test_authorization_requires_affirmative_latest_user_turn():
    assert _explicit_high_impact_authorization([
        {"role": "user", "content": "I approve the purchase; go ahead and place the order."},
    ])
    assert not _explicit_high_impact_authorization([
        {"role": "user", "content": "Do not place any order; this is only login debugging."},
    ])


def test_unapproved_high_impact_call_is_intercepted():
    call = {"function": {"name": "place_trade", "arguments": "{}"}}
    try:
        _enforce_high_impact_approval(
            [call],
            provider="xiaomi",
            model="mimo-v2.5",
            request_id="req-test",
            explicitly_authorized=False,
            greylisted=True,
        )
    except HighImpactApprovalRequiredError as exc:
        assert exc.code == "approval_required"
        assert exc.status == 428
    else:
        raise AssertionError("unapproved high-impact call was not blocked")


def test_approved_high_impact_call_is_allowed():
    reset_reputation()
    _enforce_high_impact_approval(
        [{"function": {"name": "place_trade", "arguments": "{}"}}],
        provider="trusted",
        model="model",
        request_id="req-test",
        explicitly_authorized=True,
    )


def test_repeated_suspicious_behavior_promotes_model_to_runtime_blacklist(monkeypatch):
    reset_reputation()
    monkeypatch.setenv("TUSKER_SUSPICIOUS_MODEL_THRESHOLD", "2")
    assert record_suspicious_behavior("xiaomi", "mimo-v2.5") == (1, False)
    assert record_suspicious_behavior("xiaomi", "mimo-v2.5") == (2, True)
    assert is_runtime_blacklisted("xiaomi", "mimo-v2.5")
    reset_reputation()


def test_complete_response_guardrail_blocks_unauthorized_trade():
    response = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "task",
                        "arguments": '{"task":"purchase shares"}',
                    },
                }],
            },
        }],
    }
    with pytest.raises(HighImpactApprovalRequiredError):
        _validate_complete_tool_response(
            response,
            [{"type": "function", "function": {"name": "task"}}],
            provider="xiaomi",
            model="mimo-v2.5",
            request_id="req-test",
            require_tool_call=False,
            reject_empty=False,
            greylisted=True,
        )


def test_complete_response_guardrail_allows_login_debugging():
    response = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "browser",
                        "arguments": '{"action":"click","text":"Log in"}',
                    },
                }],
            },
        }],
    }
    assert _validate_complete_tool_response(
        response,
        [{"type": "function", "function": {"name": "browser"}}],
        provider="xiaomi",
        model="mimo-v2.5",
        request_id="req-test",
        require_tool_call=False,
        reject_empty=False,
        greylisted=True,
    ) == response
