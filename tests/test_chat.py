"""Tests for chat completions endpoint, including spec validation."""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from tusker_gateway.budget import BudgetDecision
from tusker_gateway.errors import ProviderError
from .conftest import HEADERS_AUTH, HEADERS_NO_AUTH


class _FakeSemanticCache:
    enabled = True
    config = SimpleNamespace(
        excluded_pools=("privacy",),
        require_deterministic=True,
    )

    def __init__(self, query_results=None):
        self.embed_messages = AsyncMock(return_value=[1.0, 0.0])
        self.query = AsyncMock(side_effect=query_results or [None])
        self.store = AsyncMock()

    def stats_snapshot(self):
        return {"hits": 0, "misses": 0, "writes": 0, "evictions": 0}


@pytest.mark.asyncio
async def test_chat_completions_requires_auth(client):
    resp = await client.post("/v1/chat/completions", json={"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expected_code", [
    ({}, "invalid_request"),
    ({"model": "hermes-code"}, "invalid_request"),
    ({"model": "hermes-code", "messages": []}, "invalid_messages"),
    ({"model": "hermes-code", "messages": [{"role": "user"}]}, "invalid_messages"),
    ({"model": "hermes-code", "messages": [{"role": "bot", "content": "hi"}]}, "invalid_messages"),
    ({"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": "invalid"}, "invalid_stream"),
])
async def test_chat_completions_validation(client, payload, expected_code):
    resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
    assert resp.status == 400
    data = await resp.json()
    assert data["error"]["code"] == expected_code


@pytest.mark.asyncio
async def test_chat_completions_pool_dispatch(client):
    # Mock PassthroughClient.chat
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {"id": "mock-id", "choices": [{"message": {"role": "assistant", "content": "hello"}}]}

        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}]}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        assert resp.status == 200
        data = await resp.json()
        assert data["choices"][0]["message"]["content"] == "hello"

        # Verify provider selection. Unrated candidates now start at 100.0,
        # so ranking preserves the first eligible candidate in the default
        # pool. The first light candidate is openai-codex/gpt-5.6-luna.
        args, kwargs = mock_chat.call_args
        assert args[0] == "openai-codex"
        assert args[1] == "gpt-5.6-luna"


@pytest.mark.asyncio
async def test_chat_pool_requires_tool_capability_and_forwards_tool_choice(app, client):
    pool_manager = MagicMock()
    pool_manager.select.return_value = ("openrouter", "tool-model")
    app["pool_manager"] = pool_manager

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "tool_calls"}],
        }
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "run it"}],
                "tools": [{"type": "function", "function": {"name": "bash"}}],
                "tool_choice": "required",
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    pool_manager.select.assert_called_once_with(
        "code",
        excluded=set(),
        required_input_modalities=None,
        requires_tools=True,
    )
    assert mock_chat.call_args.kwargs["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_chat_pool_image_requirement_is_preserved_across_fallbacks(app, client):
    pool_manager = MagicMock()
    pool_manager.select.side_effect = [
        ("xiaomi", "mimo-v2.5"),
        ("openai", "gpt-4o"),
    ]
    app["pool_manager"] = pool_manager

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.side_effect = [
            RuntimeError("first provider failed"),
            {"choices": [{"message": {"role": "assistant", "content": "seen"}}]},
        ]
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
                    ],
                }],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    assert pool_manager.select.call_args_list == [
        call("code", excluded=set(), required_input_modalities=frozenset({"image"})),
        call(
            "code",
            excluded={("xiaomi", "mimo-v2.5")},
            required_input_modalities=frozenset({"image"}),
        ),
    ]


@pytest.mark.asyncio
async def test_chat_stream_provider_502_falls_back_before_client_response(app, client):
    """A stream setup 502 must select a fallback before sending HTTP 200."""
    pool_manager = MagicMock()
    pool_manager.select.side_effect = [
        ("openrouter", "nvidia/saturated-model"),
        ("openai", "gpt-4o-mini"),
    ]
    app["pool_manager"] = pool_manager

    failure = ProviderError(
        "Upstream error from Nvidia: ResourceExhausted: worker limit reached",
        code="provider_error",
    )
    failure.upstream_status = 502
    failure.upstream_body = '{"error":{"code":502}}'

    async def fallback_stream(*args, **kwargs):
        yield b'data: {"choices":[{"delta":{"content":"fallback"}}]}\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.side_effect = [failure, fallback_stream()]
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers=HEADERS_AUTH,
        )
        content = await resp.read()

    assert resp.status == 200
    assert b"fallback" in content
    assert pool_manager.select.call_args_list == [
        call("code", excluded=set(), required_input_modalities=None),
        call(
            "code",
            excluded={("openrouter", "nvidia/saturated-model")},
            required_input_modalities=None,
        ),
    ]


@pytest.mark.asyncio
async def test_chat_pool_input_image_requires_image_after_breaker_skip(app, client):
    pool_manager = MagicMock()
    pool_manager.select.side_effect = [
        ("xiaomi", "mimo-v2.5-pro"),
        ("xiaomi", "mimo-v2.5"),
    ]
    app["pool_manager"] = pool_manager
    breaker = MagicMock()
    breaker.check.side_effect = [
        MagicMock(allowed=False),
        MagicMock(allowed=True),
    ]
    app["breaker"] = breaker

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "seen"}}],
        }
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "input_image",
                        "image_url": "data:image/png;base64,AAAA",
                    }],
                }],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    assert pool_manager.select.call_args_list == [
        call("code", excluded=set(), required_input_modalities=frozenset({"image"})),
        call(
            "code",
            excluded={("xiaomi", "mimo-v2.5-pro")},
            required_input_modalities=frozenset({"image"}),
        ),
    ]


@pytest.mark.asyncio
async def test_chat_text_only_pool_request_has_no_modality_requirement(app, client):
    pool_manager = MagicMock()
    pool_manager.select.return_value = ("openai", "gpt-4o-mini")
    app["pool_manager"] = pool_manager

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        }
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    pool_manager.select.assert_called_once_with(
        "code",
        excluded=set(),
        required_input_modalities=None,
    )


@pytest.mark.asyncio
async def test_semantic_cache_uses_concrete_route_and_does_not_cross_hit(app, client):
    semantic = _FakeSemanticCache([
        None,
        {"id": "cached", "choices": [{"message": {"content": "cached answer"}}]},
    ])
    app["semantic_cache"] = semantic
    pool_manager = MagicMock()
    pool_manager.select.return_value = ("openai", "gpt-4o-mini")
    app["pool_manager"] = pool_manager

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "id": "upstream",
            "choices": [{"message": {"role": "assistant", "content": "fresh answer"}}],
        }
        payload = {
            "model": "hermes-code",
            "messages": [{"role": "user", "content": "answer this"}],
            "temperature": 0,
        }
        first = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        second = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)

    assert first.status == 200
    assert second.status == 200
    assert (await second.json())["id"] == "cached"
    mock_chat.assert_awaited_once()
    assert semantic.store.await_count == 1
    assert semantic.query.await_count == 2
    assert semantic.query.call_args_list[0].kwargs["scope"] == semantic.query.call_args_list[1].kwargs["scope"]
    assert semantic.store.call_args.kwargs["scope"] == semantic.query.call_args_list[0].kwargs["scope"]
    assert mock_chat.call_args.args[:2] == ("openai", "gpt-4o-mini")


@pytest.mark.asyncio
async def test_semantic_cache_requires_explicit_deterministic_request(app, client):
    semantic = _FakeSemanticCache()
    app["semantic_cache"] = semantic
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "fresh"}}],
        }
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "openai::gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    semantic.embed_messages.assert_not_awaited()
    semantic.query.assert_not_awaited()
    semantic.store.assert_not_awaited()
    mock_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_cache_skips_structured_output_requests(app, client):
    semantic = _FakeSemanticCache()
    app["semantic_cache"] = semantic
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "{}"}}],
        }
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "openai::gpt-4o-mini",
                "messages": [{"role": "user", "content": "return JSON"}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    semantic.embed_messages.assert_not_awaited()
    semantic.query.assert_not_awaited()
    semantic.store.assert_not_awaited()
    mock_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_cache_skips_any_zdr_pool_even_without_name(app, client):
    semantic = _FakeSemanticCache()
    app["semantic_cache"] = semantic
    app["config"]["pools"]["code"].zdr = True
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "fresh"}}],
        }
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "private"}],
                "temperature": 0,
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    semantic.embed_messages.assert_not_awaited()
    semantic.query.assert_not_awaited()
    semantic.store.assert_not_awaited()
    mock_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_cache_never_handles_tool_requests(app, client):
    semantic = _FakeSemanticCache()
    app["semantic_cache"] = semantic
    pool_manager = MagicMock()
    pool_manager.select.return_value = ("openai", "tool-model")
    app["pool_manager"] = pool_manager
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{
                "message": {"tool_calls": [{"id": "call-1"}]},
                "finish_reason": "tool_calls",
            }],
        }
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-code",
                "messages": [{"role": "user", "content": "run it"}],
                "tools": [{"type": "function", "function": {"name": "bash"}}],
                "tool_choice": "required",
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    semantic.embed_messages.assert_not_awaited()
    semantic.query.assert_not_awaited()
    semantic.store.assert_not_awaited()
    mock_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_budget_rejection_happens_before_semantic_cache(app, client):
    semantic = _FakeSemanticCache([
        {"id": "must-not-be-used", "choices": [{"message": {"content": "cached"}}]},
    ])
    app["semantic_cache"] = semantic
    budget = MagicMock()
    budget.check.return_value = BudgetDecision(
        allowed=False,
        reason="daily cap exceeded",
        cap_name="daily",
    )
    app["budget"] = budget
    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "openai::gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0,
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 429
    semantic.embed_messages.assert_not_awaited()
    semantic.query.assert_not_awaited()
    mock_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_image_passthrough_does_not_select_from_pool(app, client):
    pool_manager = MagicMock()
    app["pool_manager"] = pool_manager

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "seen"}}],
        }
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "openai::gpt-4o",
                "messages": [{
                    "role": "user",
                    "content": [{
                        "type": "image_url",
                        "image_url": {"url": "https://example.test/image.png"},
                    }],
                }],
            },
            headers=HEADERS_AUTH,
        )

    assert resp.status == 200
    pool_manager.select.assert_not_called()
    assert mock_chat.call_args.args[:2] == ("openai", "gpt-4o")


@pytest.mark.asyncio
async def test_chat_completions_streaming(client):
    # Mock streaming response. The endpoint sends [DONE] itself on EOF, so
    # the upstream mock only yields content chunks here.
    async def mock_upstream_stream(*args, **kwargs):
        yield b'data: {"content": "a"}\n\n'
        yield b'data: {"content": "b"}\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = mock_upstream_stream()

        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/event-stream"
        # New hardening headers: disables nginx/traefik proxy buffering so
        # heartbeats and incremental chunks reach the client immediately.
        assert resp.headers.get("X-Accel-Buffering") == "no"

        content = await resp.read()
        lines = content.splitlines()
        assert b'data: {"content": "a"}' in lines
        assert b'data: {"content": "b"}' in lines
        assert b'data: [DONE]' in lines


@pytest.mark.asyncio
async def test_chat_completions_streaming_emits_role_chunk_first(client):
    """First bytes must be a parseable role chunk so clients have immediate signal."""
    async def mock_upstream_stream(*args, **kwargs):
        yield b'data: {"content": "hello"}\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = mock_upstream_stream()
        payload = {"model": "hermes-code", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        content = await resp.read()

    # First SSE event must be a role chunk (per OpenAI streaming reference).
    first_event = content.split(b"\n\n", 1)[0]
    assert first_event.startswith(b"data: ")
    payload_json = json.loads(first_event[len(b"data: "):])
    assert payload_json["choices"][0]["delta"]["role"] == "assistant"


@pytest.mark.asyncio
async def test_chat_completions_streaming_emits_heartbeat_when_upstream_is_slow(
    client, monkeypatch,
):
    """If the upstream pauses between chunks, SSE keepalives must keep flowing.

    This is the regression test for the "socket connection was closed
    unexpectedly" symptom: without periodic comments, idle-connection
    timers on Traefik/Cloudflare/CloudFront would expire and rebalance
    the TCP socket underneath us.

    Note: the aiohttp test client buffers the response before `read()`
    returns, so we count heartbeats in the *final* body — the endpoint's
    heartbeat task writes them as it runs, regardless of when the test
    client decides to flush them downstream.
    """
    # Shrink the heartbeat interval so the test stays fast.
    monkeypatch.setenv("TUSKER_SSE_HEARTBEAT_SECS", "0.05")

    async def slow_upstream(*args, **kwargs):
        # First chunk arrives immediately, then a long silence.
        yield b'data: {"content": "first"}\n\n'
        await asyncio.sleep(0.3)  # 6 heartbeat ticks worth
        yield b'data: {"content": "second"}\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = slow_upstream()
        payload = {
            "model": "hermes-code",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        content = await resp.read()

    # Count SSE comment lines.
    heartbeat_count = content.count(b": keepalive\n")
    assert heartbeat_count >= 2, (
        f"expected at least 2 keepalive comments during the 0.3s upstream pause, "
        f"got {heartbeat_count}; full body:\n{content!r}"
    )
    # And the actual chunks still made it through.
    assert b'data: {"content": "first"}' in content
    assert b'data: {"content": "second"}' in content
    assert b'data: [DONE]' in content


@pytest.mark.asyncio
async def test_chat_completions_streaming_no_heartbeat_when_disabled(client, monkeypatch):
    """TUSKER_SSE_HEARTBEAT_SECS=0 must turn off keepalives entirely."""
    monkeypatch.setenv("TUSKER_SSE_HEARTBEAT_SECS", "0")

    async def fast_upstream(*args, **kwargs):
        yield b'data: {"content": "x"}\n\n'

    with patch("tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = fast_upstream()
        payload = {
            "model": "hermes-code",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        resp = await client.post("/v1/chat/completions", json=payload, headers=HEADERS_AUTH)
        content = await resp.read()

    # No keepalives should have been emitted.
    assert b": keepalive" not in content
    assert b'data: {"content": "x"}' in content
    assert b'data: [DONE]' in content
