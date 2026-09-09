"""End-to-end backend tool passthrough tests with mocked providers."""
from __future__ import annotations
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import pytest
from tusker_gateway.passthrough import PassthroughClient
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from .conftest import HEADERS_AUTH

from tusker_gateway.quality import QualityDB
from tusker_gateway.tool_formats import (
    normalize_tools, normalize_tool_calls, openai_messages_to_anthropic,
    parse_text_tool_calls,
)


def _cfg(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "api_keys": ["gw-test-key"],
        "provider_api_keys": {},
        "codex_credentials": [],
        "quality_db_path": ":memory:",
    }
    cfg.update(overrides)
    return cfg


def _mock_response(payload: dict[str, Any]):
    """Build an async context manager that yields a response with given JSON payload."""
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_http(json_payload: dict[str, Any]) -> MagicMock:
    http = MagicMock()
    http.request = MagicMock(return_value=_mock_response(json_payload))
    return http


@pytest.mark.asyncio
async def test_anthropic_xml_text_rescued_to_tool_calls():
    text = '<tool_call>\n{"name":"bash","arguments":{"command":"ls"}}\n</tool_call>'
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})
    client = PassthroughClient(_cfg(), QualityDB(":memory:"), http)
    result = await client.chat("openai", "gpt-4o", [{"role": "user", "content": "hi"}],
                               tools=[{"type": "function", "function": {"name": "bash"}}])
    msg = result["choices"][0]["message"]
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "bash"
    assert "command" in json.loads(msg["tool_calls"][0]["function"]["arguments"])
    assert result["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_claude_invoke_rescued_from_text():
    text = '<function_calls><invoke name="bash"><parameter name="command">ls</parameter></invoke></function_calls>'
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})
    client = PassthroughClient(_cfg(), QualityDB(":memory:"), http)
    result = await client.chat("openai", "gpt-4o", [{"role": "user", "content": "hi"}],
                               tools=[{"type": "function", "function": {"name": "bash"}}])
    assert len(result["choices"][0]["message"]["tool_calls"]) == 1


@pytest.mark.asyncio
async def test_native_tool_calls_passthrough_unchanged():
    tc = {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path":"/tmp"}'}}
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [tc]}, "finish_reason": "tool_calls"}]})
    client = PassthroughClient(_cfg(), QualityDB(":memory:"), http)
    result = await client.chat("openai", "gpt-4o", [{"role": "user", "content": "hi"}],
                               tools=[{"type": "function", "function": {"name": "read"}}])
    assert result["choices"][0]["message"]["tool_calls"][0]["id"] == "c1"


@pytest.mark.asyncio
async def test_bedrock_tool_use_normalized():
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": [
        {"type": "text", "text": "ok"},
        {"toolUse": {"toolUseId": "b1", "name": "bash", "input": {"command": "pwd"}}},
    ]}, "finish_reason": "stop"}]})
    client = PassthroughClient(_cfg(), QualityDB(":memory:"), http)
    result = await client.chat("openai", "gpt-4o", [{"role": "user", "content": "hi"}],
                               tools=[{"type": "function", "function": {"name": "bash"}}])
    msg = result["choices"][0]["message"]
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "bash"


@pytest.mark.asyncio
async def test_text_only_response_no_tool_calls():
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}]})
    client = PassthroughClient(_cfg(), QualityDB(":memory:"), http)
    result = await client.chat("openai", "gpt-4o", [{"role": "user", "content": "hi"}],
                               tools=[{"type": "function", "function": {"name": "bash"}}])
    assert "tool_calls" not in result["choices"][0]["message"]
    assert result["choices"][0]["message"]["content"] == "hello"


@pytest.mark.asyncio
async def test_oauth_provider_sets_auth_header_with_tools():
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    cfg = _cfg(codex_credentials=[{"access_token": "tok123"}])
    client = PassthroughClient(cfg, QualityDB(":memory:"), http)
    await client.chat("github-copilot", "gpt-4o", [{"role": "user", "content": "hi"}],
                      tools=[{"type": "function", "function": {"name": "bash"}}])
    headers = http.request.call_args.kwargs.get("headers") or http.request.call_args[1].get("headers")
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer")


@pytest.mark.asyncio
async def test_request_body_normalizes_tools_for_bearer_provider():
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    client = PassthroughClient(_cfg(provider_api_keys={"openai": "sk-test"}), QualityDB(":memory:"), http)
    await client.chat("openai", "gpt-4o", [{"role": "user", "content": "hi"}],
                      tools=[{"function": {"name": "bash"}}])
    body = http.request.call_args.kwargs.get("json")
    assert "tools" in body
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "bash"


@pytest.mark.asyncio
async def test_request_body_preserves_explicit_tool_choice():
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    client = PassthroughClient(_cfg(provider_api_keys={"openai": "sk-test"}), QualityDB(":memory:"), http)
    await client.chat(
        "openai",
        "gpt-4o",
        [{"role": "user", "content": "hi"}],
        tools=[{"function": {"name": "bash"}}],
        tool_choice="required",
    )
    body = http.request.call_args.kwargs.get("json")
    assert body["tool_choice"] == "required"


@pytest.mark.parametrize("provider", ["google", "openai"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/responses"])
async def test_google_store_compatibility_at_upstream(app, client, provider, stream, endpoint):
    """Both API surfaces must reach upstream without losing tools or images."""
    received = []

    async def upstream(request):
        body = await request.json()
        received.append(body)
        if provider == "google" and "store" in body:
            return web.json_response({"error": {"message": "Unknown field: store"}}, status=400)
        if stream:
            return web.Response(
                content_type="text/event-stream",
                text='data: {"choices":[{"index":0,"delta":{"content":"compatible"},"finish_reason":null}]}\n\n'
                     'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                     'data: [DONE]\n\n',
            )
        return web.json_response({
            "choices": [{"message": {"role": "assistant", "content": "compatible"}, "finish_reason": "stop"}],
        })

    upstream_app = web.Application()
    upstream_app.router.add_post("/chat/completions", upstream)
    model = "gemini-3.1-flash-lite" if provider == "google" else "gpt-4o"
    image_url = "data:image/png;base64,aW1hZ2U="
    function = {"name": "describe", "parameters": {"type": "object", "properties": {}}}
    payload = {
        "model": f"{provider}::{model}",
        "store": False,
        "stream": stream,
        "reasoning_effort": "low",
        "tool_choice": "auto",
        "stream_options": {"include_usage": True},
        "response_format": {"type": "json_object"},
    }
    if endpoint == "/v1/responses":
        payload["input"] = [{"role": "user", "content": [
            {"type": "input_text", "text": "Describe this image"},
            {"type": "input_image", "image_url": image_url},
        ]}]
        payload["tools"] = [{"type": "function", **function}]
    else:
        payload["messages"] = [{"role": "user", "content": [
            {"type": "text", "text": "Describe this image"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]}]
        payload["tools"] = [{"type": "function", "function": function}]

    async with TestServer(upstream_app) as server, ClientSession() as session:
        app["config"]["providers"] = {provider: {
            "base_url": str(server.make_url("/")).rstrip("/"),
            "chat_path": "/chat/completions",
            "auth_type": "bearer",
        }}
        app["config"]["provider_api_keys"] = {provider: "upstream-test-key"}
        app["config"]["quality_db_path"] = ":memory:"
        app["http_session"] = session
        response = await client.post(endpoint, json=payload, headers=HEADERS_AUTH)
        result = await response.text()

    assert response.status == 200, result
    assert "compatible" in result
    assert len(received) == 1
    body = received[0]
    if provider == "google":
        assert "store" not in body
    else:
        assert body["store"] is False
    assert body["model"] == model
    assert body["stream"] is stream
    assert body["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "Describe this image"},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]}]
    assert body["tools"] == [{"type": "function", "function": {
        "name": "describe",
        "description": "",
        "parameters": {"type": "object", "properties": {}},
    }}]
    assert body["tool_choice"] == "auto"
    assert body["reasoning_effort"] == "low"
    assert body["stream_options"] == {"include_usage": True}
    assert body["response_format"] == {"type": "json_object"}

async def test_opencode_go_sets_conversation_session_header():
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    client = PassthroughClient(
        _cfg(provider_api_keys={"opencode-go": "go-test-key"}),
        QualityDB(":memory:"),
        http,
    )

    await client.chat(
        "opencode-go",
        "minimax-m3",
        [{"role": "user", "content": "hello"}],
        conversation_id="omp-conversation-123",
    )

    request = http.request.call_args
    headers = request.kwargs["headers"]
    assert headers["x-opencode-session"] == "omp-conversation-123"
    assert request.args[0] == "POST"
    assert request.args[1].endswith("/zen/go/v1/chat/completions")


@pytest.mark.asyncio
async def test_opencode_zen_sets_conversation_session_header():
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    client = PassthroughClient(
        _cfg(provider_api_keys={"opencode-zen": "zen-test-key"}),
        QualityDB(":memory:"),
        http,
    )

    await client.chat(
        "opencode-zen",
        "big-pickle",
        [{"role": "user", "content": "hello"}],
        conversation_id="omp-conversation-123",
    )

    request = http.request.call_args
    headers = request.kwargs["headers"]
    assert headers["x-opencode-session"] == "omp-conversation-123"
    assert request.args[0] == "POST"
    assert request.args[1].endswith("/zen/v1/chat/completions")


@pytest.mark.asyncio
async def test_opencode_go_fallback_session_is_stable_as_history_grows():
    first_turn = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Start this conversation."},
    ]
    later_turn = [
        *first_turn,
        {"role": "assistant", "content": "Started."},
        {"role": "user", "content": "Continue it."},
    ]
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    client = PassthroughClient(
        _cfg(provider_api_keys={"opencode-go": "go-test-key"}),
        QualityDB(":memory:"),
        http,
    )

    await client.chat("opencode-go", "minimax-m3", first_turn)
    first_header = http.request.call_args.kwargs["headers"]["x-opencode-session"]
    await client.chat("opencode-go", "minimax-m3", later_turn)
    later_header = http.request.call_args.kwargs["headers"]["x-opencode-session"]

    assert first_header == later_header
    assert first_header.startswith("tusker-")


@pytest.mark.asyncio
async def test_opencode_go_normalizes_openai_reasoning_effort_aliases():
    http = _mock_http({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    client = PassthroughClient(
        _cfg(provider_api_keys={"opencode-go": "go-test-key"}),
        QualityDB(":memory:"),
        http,
    )

    await client.chat(
        "opencode-go",
        "minimax-m3",
        [{"role": "user", "content": "hello"}],
        extra_body={"reasoning_effort": "minimal"},
    )

    body = http.request.call_args.kwargs["json"]
    assert body["reasoning_effort"] == "low"


def test_openai_messages_to_anthropic_round_trip():
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "thinking", "tool_calls": [
            {"id": "c1", "function": {"name": "bash", "arguments": '{"command":"ls"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "file1.txt"},
    ]
    res = openai_messages_to_anthropic(msgs)
    assert res[0]["role"] == "user"  # system → user
    assert res[1]["role"] == "user"
    assert res[2]["role"] == "assistant"
    blocks = res[2]["content"]
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["input"]["command"] == "ls"
    assert res[3]["content"][0]["tool_use_id"] == "c1"


def test_dsml_namespaced_xml():
    text = '<dsml:invoke name="s"><parameter name="arg1">value1</parameter></dsml:invoke>'
    calls = parse_text_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "s"


def test_anthropic_tool_choice_maps_to_openai_required():
    from tusker_gateway.translators.anthropic import request_anthropic_to_openai

    converted = request_anthropic_to_openai({
        "model": "hermes-code",
        "messages": [{"role": "user", "content": "run it"}],
        "tools": [{
            "name": "bash",
            "description": "run a command",
            "input_schema": {"type": "object", "properties": {}},
        }],
        "tool_choice": {"type": "any"},
    })
    assert converted["tool_choice"] == "required"


def test_tool_invocation_self_closing():
    text = '<tool_invocation name="bash" arguments={"command":"echo hi"} />'
    calls = parse_text_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "bash"


def test_tool_call_text_with_leading_text():
    text = 'Sure, let me run that.\n\n<tool_call>\n{"name":"bash","arguments":{"command":"ls"}}\n</tool_call>'
    calls = parse_text_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "bash"


def test_normalize_tools_strips_empty_name():
    tools = [{"function": {"name": "", "description": "empty"}}]
    assert normalize_tools(tools) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,status", [(False, 404), (True, 404), (True, 410)])
async def test_first_unavailable_response_excludes_pool_route(tmp_path, monkeypatch, stream, status):
    from types import SimpleNamespace
    from tusker_gateway import cooldown
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.errors import ProviderError
    from tusker_gateway.pools import PoolManager

    monkeypatch.setattr(cooldown, "PERMANENTLY_FAILED_MODELS", {})
    route = ("google", "gemini-2.5-pro")
    calls = []

    async def unavailable(request):
        calls.append(await request.json())
        return web.json_response({"error": {"message": "Model unavailable"}}, status=status)

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", unavailable)
    config = _cfg(
        quality_db_path=str(tmp_path / "quality.db"),
        provider_api_keys={"google": "test-key"},
        pools={"premium": PoolConfig(name="premium", models=[
            {"provider": route[0], "model": route[1]},
        ])},
    )
    manager = PoolManager(config)
    assert manager.select("premium", session_id="sticky") == route
    async with TestServer(upstream) as server, ClientSession() as http:
        client = PassthroughClient(config, QualityDB(config["quality_db_path"]), http)
        with pytest.raises(ProviderError) as failure:
            await client.chat(*route, [{"role": "user", "content": "hello"}],
                              stream=stream, upstream_gateway=str(server.make_url("/")))
        assert failure.value.upstream_status == status

    assert len(calls) == 1
    # Neither ordinary selection, stickiness nor recovery may retry the dead route.
    assert manager.select("premium") is None
    assert manager.select("premium", session_id="sticky") is None
    assert manager.select("premium", allow_cooldown_probe=True) is None
    assert manager.readiness_status()[0]["premium"]["selectable"] == 0
    assert manager.status()["premium"]["valid_candidates"] == 0
    monkeypatch.setattr(cooldown, "time", SimpleNamespace(monotonic=lambda: 10**12))
    assert manager.select("premium", allow_cooldown_probe=True) is None
    cooldown.clear_permanently_failed(*route)
    assert manager.select("premium", allow_cooldown_probe=True) == route
