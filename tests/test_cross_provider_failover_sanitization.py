"""Cross-provider failover must not leak opaque replay state upstream.

The runbook contract: opaque provider signatures and reasoning channels are
preserved for same-provider continuation, stripped (copy-on-write) when a
failover candidate runs on a different upstream provider, and the caller's
shared conversation history is never mutated.
"""
from __future__ import annotations

import pytest

from tusker_gateway.endpoints import _sanitize_cross_provider_messages


def _history() -> list[dict]:
    return [
        {"role": "user", "content": "list files"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "need the bash tool"},
                {"type": "text", "text": "Running ls."},
            ],
            "reasoning_content": "need the bash tool",
            "thought_signature": "opaque-from-upstream-a",
            "tool_calls": [{
                "id": "call_9", "type": "function",
                "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"},
                "thought_signature": "tool-sig-a",
            }],
        },
        {"role": "tool", "tool_call_id": "call_9", "content": "file.txt"},
    ]


def test_cross_provider_strips_reasoning_and_signatures():
    sanitized = _sanitize_cross_provider_messages(_history())
    assistant = sanitized[1]
    assert "reasoning_content" not in assistant
    assert "thought_signature" not in assistant
    # thinking content blocks are dropped; text survives
    assert assistant["content"] == [{"type": "text", "text": "Running ls."}]
    # tool call structure preserved (id + arguments) without opaque keys
    call = assistant["tool_calls"][0]
    assert call["id"] == "call_9"
    assert call["function"] == {"name": "bash", "arguments": "{\"command\":\"ls\"}"}
    assert "thought_signature" not in call
    # non-assistant turns pass through intact
    assert sanitized[0] == {"role": "user", "content": "list files"}
    assert sanitized[2]["tool_call_id"] == "call_9"


def test_original_history_never_mutated():
    history = _history()
    _sanitize_cross_provider_messages(history)
    assert history[1]["reasoning_content"] == "need the bash tool"
    assert history[1]["thought_signature"] == "opaque-from-upstream-a"
    assert history[1]["content"][0]["type"] == "thinking"


def test_sanitized_messages_are_new_objects():
    history = _history()
    sanitized = _sanitize_cross_provider_messages(history)
    assert sanitized is not history
    assert all(a is not b for a, b in zip(sanitized, history))


@pytest.mark.asyncio
async def test_pool_failover_sanitizes_only_cross_provider(monkeypatch):
    """In _call_with_pool_fallback, same-provider retry keeps opaque fields;
    a different-provider candidate gets the sanitized copy."""
    from unittest.mock import AsyncMock, MagicMock

    import tusker_gateway.endpoints as endpoints

    calls: list[tuple[str, list[dict]]] = []

    class _FakeClient:
        async def chat(self, provider, model, messages, **kwargs):
            calls.append((provider, messages))
            if provider == "alpha":
                raise endpoints.ProviderError("alpha down")
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "done"}}]}

    config = {
        "api_keys": ["sk-test"],
        "providers": {},
        "models": {},
        "quality_db_path": "/tmp/x.db",
        "max_tokens": 100,
    }
    body = {
        "model": "pool-a",
        "messages": _history(),
        "stream": False,
    }

    monkeypatch.setattr(endpoints, "PassthroughClient", _FakeClient, raising=False)
    monkeypatch.setattr(endpoints, "_provider_attempt_timeout_secs",
                        lambda request, **k: 5.0, raising=False)
    monkeypatch.setattr(endpoints, "_prepare_stream_result",
                        AsyncMock(return_value={"ok": True}), raising=False)
    monkeypatch.setattr(endpoints, "_build_extra_body",
                        lambda body: {}, raising=False)
    monkeypatch.setattr(endpoints, "_candidate_extra_body",
                        lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(endpoints, "estimate_prompt_tokens", lambda *a, **k: 10,
                        raising=False)
    monkeypatch.setattr(endpoints, "_required_input_modalities",
                        lambda messages: frozenset(), raising=False)
    monkeypatch.setattr(endpoints, "_pool_name", lambda body: "pool-a", raising=False)
    monkeypatch.setattr(endpoints, "_requires_structured_output",
                        lambda body, pool: False, raising=False)
    monkeypatch.setattr(endpoints, "tools_include_high_impact", lambda tools: False,
                        raising=False)
    monkeypatch.setattr(endpoints, "_max_pool_provider_attempts", lambda: 6,
                        raising=False)

    class _FakePoolManager:
        def __init__(self, config):
            pass

        def fallback_pools(self, pool_name):
            return ()

        def select(self, pool_name, **kwargs):
            excluded = kwargs.get("excluded") or set()
            if ("alpha", "m1") not in excluded:
                return ("alpha", "m1")
            return ("beta", "m2")

    monkeypatch.setattr(endpoints, "PoolManager", _FakePoolManager, raising=False)

    provider, model, result = await endpoints._call_with_pool_fallback(
        config, body, _FakeClient(),
    )
    assert (provider, model) == ("beta", "m2")
    # First call (alpha): full history with opaque fields.
    first_provider, first_messages = calls[0]
    assert first_provider == "alpha"
    assert first_messages[1]["reasoning_content"] == "need the bash tool"
    # Second call (beta): sanitized copy, opaque fields gone.
    second_provider, second_messages = calls[1]
    assert second_provider == "beta"
    assert "reasoning_content" not in second_messages[1]
    assert "thought_signature" not in second_messages[1]
    # And the original body history is untouched for the client.
    assert body["messages"][1]["reasoning_content"] == "need the bash tool"
