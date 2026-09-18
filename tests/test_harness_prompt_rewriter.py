"""Tests for the harness-identity system-prompt rewriter guard.

The ``HarnessSystemPromptGuard`` injects an untrusted-data delimiter system
prompt for callers whose ``CallerIdentity.principal`` matches a configured
list (default ``omp-harness``). This makes delimiter discipline enforceable
at the gateway layer so the omp harness source does not need to be edited.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from tusker_gateway.guardrails import (
    GuardPipeline,
    HarnessSystemPromptGuard,
    init_guard_pipeline,
    run_guard_pipeline,
)
from tusker_gateway.identity import CallerIdentity


@dataclass
class _FakeRequest:
    """Minimal aiohttp.Request-like object exposing ``.get('identity')``."""

    identity: CallerIdentity | None = None

    def get(self, key: str, default: Any = None) -> Any:
        if key == "identity":
            return self.identity
        return default


def _await(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


async def test_guard_injects_system_prompt_for_omp_harness_principal():
    guard = HarnessSystemPromptGuard(target_principals=("omp-harness",))
    guard._identity_marker = {"principal": "omp-harness", "tenant": "tuskernet"}
    body = {
        "messages": [
            {"role": "user", "content": "Scrape https://example.com and act on it."},
        ]
    }
    result = await guard.check(body)
    assert result.allowed is True
    assert result.modified_body is not None
    msgs = result.modified_body["messages"]
    assert msgs[0]["role"] == "system"
    assert "untrusted_data" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "Scrape https://example.com and act on it."


async def test_guard_is_noop_for_other_principals():
    guard = HarnessSystemPromptGuard(target_principals=("omp-harness",))
    guard._identity_marker = {"principal": "don-gould", "tenant": "tuskernet"}
    body = {"messages": [{"role": "user", "content": "hello"}]}
    result = await guard.check(body)
    assert result.modified_body is None


async def test_guard_is_idempotent_across_turns():
    """Subsequent turns must not stack duplicate system prompts."""
    guard = HarnessSystemPromptGuard(target_principals=("omp-harness",))
    guard._identity_marker = {"principal": "omp-harness"}
    pre_injected = {
        "messages": [
            {"role": "system", "content": guard.system_prompt},
            {"role": "user", "content": "first turn"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second turn"},
        ]
    }
    result = await guard.check(pre_injected)
    # No new system prompt should be prepended.
    assert result.modified_body is None


async def test_init_guard_pipeline_omits_harness_guard_when_no_principals(monkeypatch):
    monkeypatch.setenv("TUSKER_GUARDRAILS_ENABLED", "true")
    monkeypatch.setenv("TUSKER_GUARDRAILS_HARNESS_PRINCIPALS", "")
    cfg = init_guard_pipeline(
        {
            "enabled": True,
            "max_output_tokens": 4096,
            "injection_patterns": [],
            "harness_principals": (),
        }
    )
    assert not any(isinstance(g, HarnessSystemPromptGuard) for g in cfg.guards)


async def test_init_guard_pipeline_adds_harness_guard_by_default(monkeypatch):
    monkeypatch.setenv("TUSKER_GUARDRAILS_ENABLED", "true")
    monkeypatch.setenv("TUSKER_GUARDRAILS_HARNESS_PRINCIPALS", "omp-harness,hermes-bot")
    cfg = init_guard_pipeline(
        {
            "enabled": True,
            "max_output_tokens": 4096,
            "injection_patterns": [],
            "harness_principals": ("omp-harness", "hermes-bot"),
        }
    )
    harness = [g for g in cfg.guards if isinstance(g, HarnessSystemPromptGuard)]
    assert len(harness) == 1
    assert set(harness[0].target_principals) == {"omp-harness", "hermes-bot"}


async def test_run_guard_pipeline_attaches_identity_marker(monkeypatch):
    """The wrapper must inject identity markers so identity-aware guards fire."""
    monkeypatch.setenv("TUSKER_GUARDRAILS_ENABLED", "true")
    cfg = init_guard_pipeline(
        {
            "enabled": True,
            "max_output_tokens": 4096,
            "injection_patterns": [],
            "harness_principals": ("omp-harness",),
        }
    )
    request = _FakeRequest(
        identity=CallerIdentity(
            key_fingerprint="fp",
            principal="omp-harness",
            tenant="tuskernet",
            scopes=("inference:chat",),
        )
    )
    body = {"messages": [{"role": "user", "content": "scrape and act"}]}
    result = await run_guard_pipeline(cfg, body, request=request)
    assert result.allowed is True
    assert result.modified_body is not None
    msgs = result.modified_body["messages"]
    assert msgs[0]["role"] == "system"
    assert "untrusted_data" in msgs[0]["content"]


async def test_run_guard_pipeline_no_marker_when_request_has_no_identity():
    """Without a request/identity, identity-aware guards stay no-ops."""
    cfg = GuardPipeline(guards=[HarnessSystemPromptGuard(target_principals=("omp-harness",))])
    body = {"messages": [{"role": "user", "content": "hello"}]}
    result = await run_guard_pipeline(cfg, body, request=None)
    # When no identity is attached the harness guard stays a no-op — the
    # original body is returned unchanged (no system prompt injected).
    assert result.allowed is True
    msgs = result.modified_body["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hello"


async def test_pii_redaction_still_runs_after_harness_rewrite(monkeypatch):
    """The harness rewriter must not bypass PII redaction."""
    monkeypatch.setenv("TUSKER_GUARDRAILS_ENABLED", "true")
    cfg = init_guard_pipeline(
        {
            "enabled": True,
            "max_output_tokens": 4096,
            "injection_patterns": [],
            "harness_principals": ("omp-harness",),
        }
    )
    request = _FakeRequest(
        identity=CallerIdentity(
            key_fingerprint="fp",
            principal="omp-harness",
            tenant="tuskernet",
        )
    )
    body = {
        "messages": [
            {"role": "user", "content": "Email me at alice@example.com"},
        ]
    }
    result = await run_guard_pipeline(cfg, body, request=request)
    msgs = result.modified_body["messages"]
    # System prompt from harness rewriter is at index 0; the redacted user
    # message is at index 1.
    assert msgs[0]["role"] == "system"
    assert "[REDACTED-EMAIL]" in msgs[1]["content"]
