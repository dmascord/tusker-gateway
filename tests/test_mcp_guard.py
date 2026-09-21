"""MCP elicitation and generic tool-guard coverage."""
from __future__ import annotations

import pytest


def _rpc(method: str, params: dict, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


async def _post(client, payload: dict):
    return await client.post("/mcp", json=payload, headers={"Authorization": "Bearer sk-secret-dev"})


@pytest.mark.asyncio
async def test_mcp_initialize_and_list_tools(client):
    response = await _post(client, _rpc("initialize", {"protocolVersion": "2026-07-28"}))
    assert response.status == 200
    body = await response.json()
    assert body["result"]["protocolVersion"] == "2026-07-28"
    assert body["result"]["capabilities"]["tools"] == {}

    response = await _post(client, _rpc("tools/list", {}))
    body = await response.json()
    assert body["result"]["tools"][0]["name"] == "tusker.guard_tool"


@pytest.mark.asyncio
async def test_mcp_initialize_falls_back_for_legacy_client(client):
    response = await _post(client, _rpc("initialize", {"protocolVersion": "2025-11-25"}))
    assert (await response.json())["result"]["protocolVersion"] == "2025-11-25"


@pytest.mark.asyncio
async def test_mcp_invalid_json_rpc_and_json_are_rejected(client):
    response = await client.post("/mcp", data="not-json", headers={"Authorization": "Bearer sk-secret-dev"})
    assert response.status == 400
    assert (await response.json())["error"]["code"] == -32700
    response = await _post(client, {"jsonrpc": "1.0", "id": 1, "method": "tools/list"})
    assert response.status == 400
    assert (await response.json())["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_mcp_safe_tool_is_allowed_without_prompt(client):
    response = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "arguments": {"tool_name": "bash", "tool_arguments": {"command": "git status"}},
        }),
    )
    body = await response.json()
    assert body["result"]["structuredContent"]["decision"] == "allow"
    assert body["result"]["isError"] is False


@pytest.mark.asyncio
async def test_mcp_destructive_tool_returns_input_required(client):
    response = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "arguments": {"tool_name": "bash", "tool_arguments": {"command": "kubectl delete pod api-1"}},
        }),
    )
    body = await response.json()
    result = body["result"]
    assert result["resultType"] == "input_required"
    assert result["inputRequests"]["approval"]["type"] == "elicitation"
    assert result["inputRequests"]["approval"]["schema"]["properties"]["approved"]["type"] == "boolean"
    assert result["requestState"]


@pytest.mark.asyncio
async def test_mcp_approval_allows_exact_original_call(client):
    first = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "arguments": {"tool_name": "place_trade", "tool_arguments": {"ticker": "ACME"}},
        }, 10),
    )
    first_body = await first.json()
    state = first_body["result"]["requestState"]
    second = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "requestState": state,
            "inputResponses": {"approval": {"action": "accept", "content": {"approved": True}}},
            "arguments": {"tool_name": "place_trade", "tool_arguments": {"ticker": "ACME"}},
        }, 11),
    )
    body = await second.json()
    assert body["result"]["structuredContent"]["decision"] == "allow"


@pytest.mark.asyncio
async def test_mcp_decline_denies_exact_original_call(client):
    first = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "arguments": {"tool_name": "send_message", "tool_arguments": {"to": "user@example.com"}},
        }, 20),
    )
    state = (await first.json())["result"]["requestState"]
    second = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "requestState": state,
            "inputResponses": {"approval": {"action": "decline"}},
            "arguments": {"tool_name": "send_message", "tool_arguments": {"to": "user@example.com"}},
        }, 21),
    )
    body = await second.json()
    assert body["result"]["structuredContent"]["decision"] == "deny"
    assert body["result"]["isError"] is True


@pytest.mark.asyncio
async def test_mcp_approval_cannot_be_reused(client):
    call = {
        "name": "tusker.guard_tool",
        "arguments": {"tool_name": "submit_order", "tool_arguments": {"id": "order-1"}},
    }
    first = await _post(client, _rpc("tools/call", call, 30))
    state = (await first.json())["result"]["requestState"]
    approved = {
        **call,
        "requestState": state,
        "inputResponses": {"approval": {"action": "accept", "content": {"approved": True}}},
    }
    response = await _post(client, _rpc("tools/call", approved, 31))
    assert (await response.json())["result"]["structuredContent"]["decision"] == "allow"
    replay = await _post(client, _rpc("tools/call", approved, 32))
    replay_body = await replay.json()
    assert replay_body["result"]["resultType"] == "input_required"


@pytest.mark.asyncio
async def test_mcp_approval_cannot_change_arguments(client):
    first = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "arguments": {"tool_name": "place_trade", "tool_arguments": {"qty": 1}},
        }, 40),
    )
    state = (await first.json())["result"]["requestState"]
    response = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "requestState": state,
            "inputResponses": {"approval": {"action": "accept", "content": {"approved": True}}},
            "arguments": {"tool_name": "place_trade", "tool_arguments": {"qty": 999}},
        }, 41),
    )
    assert (await response.json())["result"]["resultType"] == "input_required"


@pytest.mark.asyncio
async def test_mcp_tampered_state_is_not_accepted(client):
    first = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "arguments": {"tool_name": "send_message", "tool_arguments": {"body": "hello"}},
        }, 50),
    )
    state = (await first.json())["result"]["requestState"]
    tampered = state[:-1] + ("A" if state[-1] != "A" else "B")
    response = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "requestState": tampered,
            "inputResponses": {"approval": {"action": "accept", "content": {"approved": True}}},
            "arguments": {"tool_name": "send_message", "tool_arguments": {"body": "hello"}},
        }, 51),
    )
    assert (await response.json())["result"]["resultType"] == "input_required"


@pytest.mark.asyncio
async def test_mcp_malformed_approval_does_not_allow(client):
    first = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "arguments": {"tool_name": "place_trade", "tool_arguments": {"qty": 2}},
        }, 60),
    )
    state = (await first.json())["result"]["requestState"]
    response = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "requestState": state,
            "inputResponses": {"approval": {"action": "accept", "content": {"approved": "yes"}}},
            "arguments": {"tool_name": "place_trade", "tool_arguments": {"qty": 2}},
        }, 61),
    )
    assert (await response.json())["result"]["resultType"] == "input_required"


@pytest.mark.asyncio
async def test_mcp_non_object_tool_arguments_are_safe(client):
    response = await _post(
        client,
        _rpc("tools/call", {
            "name": "tusker.guard_tool",
            "arguments": {"tool_name": "read_file", "tool_arguments": "README.md"},
        }, 70),
    )
    assert (await response.json())["result"]["structuredContent"]["decision"] == "allow"


@pytest.mark.asyncio
async def test_mcp_rejects_unknown_methods_and_tools(client):
    response = await _post(client, _rpc("tools/call", {"name": "not-our-tool", "arguments": {}}))
    assert response.status == 400
    body = await response.json()
    assert body["error"]["code"] == -32602
    response = await _post(client, _rpc("unknown/method", {}))
    assert response.status == 404


@pytest.mark.asyncio
async def test_mcp_requires_auth(client):
    response = await client.post("/mcp", json=_rpc("tools/list", {}))
    assert response.status == 401
