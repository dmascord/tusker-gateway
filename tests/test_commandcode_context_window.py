"""Regression tests for Command Code context window handling.

Command Code models have internal thinking_budgets that exceed typical context
windows; a low fitted max_completion_tokens can trigger 400s such as
"max_completion_tokens [1024] must be greater than thinking_budget [24576]".

This module ensures that _builtin_context_window returns a generous window
for commandcode/* routes.
"""

from __future__ import annotations

import os

from tusker_gateway import context_limits


def test_commandcode_builtin_context_window_generous():
    for model in [
        "Qwen/Qwen4-Max",
        "MiniMaxAI/MiniMax-M2.7",
        "Qwen/Qwen3.6-Plus",
        "claude-fable-5-2",
    ]:
        window = context_limits._builtin_context_window("commandcode", model)
        assert window is not None, f"commandcode/{model} should have a builtin window"
        assert window >= 65536, f"commandcode/{model} builtin should be generous, not {window}"


def test_commandcode_env_override_wins():
    """TUSKER_CONTEXT_WINDOW_COMMANDCODE takes precedence over the default."""
    original = os.environ.pop("TUSKER_CONTEXT_WINDOW_COMMANDCODE", None)
    try:
        os.environ["TUSKER_CONTEXT_WINDOW_COMMANDCODE"] = "32768"
        assert (
            context_limits._builtin_context_window("commandcode", "Qwen/Qwen3.6-Plus")
            == 32768
        )
    finally:
        if original is not None:
            os.environ["TUSKER_CONTEXT_WINDOW_COMMANDCODE"] = original


def test_commandcode_default_when_unset():
    """Without the env var the built-in window is the 65536 default."""
    original = os.environ.pop("TUSKER_CONTEXT_WINDOW_COMMANDCODE", None)
    try:
        assert (
            context_limits._builtin_context_window("commandcode", "Qwen/Qwen3.6-Plus")
            == 65536
        )
    finally:
        if original is not None:
            os.environ["TUSKER_CONTEXT_WINDOW_COMMANDCODE"] = original


def test_other_providers_keep_their_windows():
    """The commandcode special case must not change other providers."""
    assert context_limits._builtin_context_window("apim", "gpt-5.6-luna") == 128000
