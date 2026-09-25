"""MCP tool-guard and elicitation bridge.

The gateway's OpenAI-compatible endpoints cannot carry a standards-defined
user prompt in the middle of a tool call.  This small MCP endpoint exposes the
same policy as an approval broker: clients submit the proposed tool and its
arguments, receive an MCP ``input_required`` result when approval is needed,
then retry with the signed request state and the user's structured response.

This endpoint does not execute tools.  It makes the authorization decision so
the connected client can execute the original tool only after the decision is
``allow``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from aiohttp import web

_PROTOCOL_VERSION = "2026-07-28"
_LEGACY_PROTOCOL_VERSION = "2025-11-25"
_GUARD_TOOL = "tusker.guard_tool"
_STATE_TTL_SECS = 300
_USED_STATES: set[str] = set()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _approval_key() -> bytes:
    """Resolve a stable signing key.

    Requires TUSKER_APPROVAL_HMAC_KEY or TUSKER_AUDIT_HMAC_KEY to be
    configured. Raises RuntimeError if neither is set.
    """
    raw = os.environ.get("TUSKER_APPROVAL_HMAC_KEY") or os.environ.get("TUSKER_AUDIT_HMAC_KEY")
    if not raw:
        raise RuntimeError(
            "TUSKER_APPROVAL_HMAC_KEY or TUSKER_AUDIT_HMAC_KEY must be "
            "configured when MCP guard is in use"
        )
    return hashlib.sha256(raw.encode()).digest()


def _encode_state(*, tool_name: str, arguments: Any, action: str, request_id: str) -> str:
    payload = {
        "v": 1,
        "nonce": secrets.token_urlsafe(12),
        "exp": int(time.time()) + _STATE_TTL_SECS,
        "tool": tool_name,
        "args_sha256": hashlib.sha256(_json_bytes(arguments)).hexdigest(),
        "action": action,
        "request_id": request_id,
    }
    encoded = base64.urlsafe_b64encode(_json_bytes(payload)).rstrip(b"=")
    signature = hmac.new(_approval_key(), encoded, hashlib.sha256).digest()
    return encoded.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def _decode_state(state: Any) -> dict[str, Any] | None:
    if not isinstance(state, str) or "." not in state:
        return None
    encoded, encoded_sig = state.split(".", 1)
    try:
        supplied = base64.urlsafe_b64decode(encoded_sig + "===")
        expected = hmac.new(_approval_key(), encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "===").decode())
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def _consume_state(state: str) -> bool:
    """Consume a valid approval state once to prevent approval replay."""
    if state in _USED_STATES:
        return False
    _USED_STATES.add(state)
    if len(_USED_STATES) > 4096:
        _USED_STATES.clear()
    return True


def _approval_content(input_responses: Any) -> tuple[bool, bool]:
    """Return (present, approved) for modern and legacy MCP response shapes."""
    if not isinstance(input_responses, dict):
        return False, False
    response = input_responses.get("approval")
    if not isinstance(response, dict):
        return False, False
    action = str(response.get("action") or "").lower()
    content = response.get("content")
    if action in {"decline", "cancel"}:
        return True, False
    if action not in {"accept", "accepted", "approve", "approved"}:
        return False, False
    if isinstance(content, dict):
        approved = content.get("approved")
        return (True, approved) if isinstance(approved, bool) else (False, False)
    # A few pre-final MCP clients return the form object directly.
    approved = response.get("approved")
    return (True, approved) if isinstance(approved, bool) else (False, False)


def _classify(tool_name: str, arguments: Any) -> str | None:
    """Use the gateway's central high-impact classifier for all tool names."""
    from tusker_gateway.endpoints import _high_impact_call_kind

    return _high_impact_call_kind(
        {"function": {"name": tool_name, "arguments": arguments}}
    )


def _jsonrpc(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(*, decision: str, tool_name: str, action: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"decision": decision, "tool": tool_name}
    if action:
        value["action"] = action
    return {
        "content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}],
        "structuredContent": value,
        "isError": decision == "deny",
    }


def _guard_call(request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    args = params.get("arguments")
    if not isinstance(args, dict):
        return _jsonrpc_error(request_id, -32602, "tools/call arguments must be an object")
    tool_name = str(args.get("tool_name") or "").strip()
    if not tool_name or tool_name == _GUARD_TOOL:
        return _jsonrpc_error(request_id, -32602, "tool_name must identify the original tool")
    tool_arguments = args.get("tool_arguments", {})
    action = _classify(tool_name, tool_arguments)
    if action is None:
        return _jsonrpc(request_id, _tool_result(decision="allow", tool_name=tool_name))

    state = _decode_state(params.get("requestState"))
    present, approved = _approval_content(params.get("inputResponses"))
    state_matches = bool(
        state
        and state.get("tool") == tool_name
        and state.get("action") == action
        and state.get("args_sha256") == hashlib.sha256(_json_bytes(tool_arguments)).hexdigest()
    )
    if present and state_matches and _consume_state(str(params.get("requestState"))):
        return _jsonrpc(
            request_id,
            _tool_result(
                decision="allow" if approved else "deny",
                tool_name=tool_name,
                action=action,
            ),
        )
    request_state = _encode_state(
        tool_name=tool_name,
        arguments=tool_arguments,
        action=action,
        request_id=str(request_id),
    )
    return _jsonrpc(
        request_id,
        {
            "resultType": "input_required",
            "inputRequests": {
                "approval": {
                    "type": "elicitation",
                    "message": f"Allow high-impact tool action '{action}' from '{tool_name}'?",
                    "schema": {
                        "type": "object",
                        "properties": {"approved": {"type": "boolean"}},
                        "required": ["approved"],
                    },
                }
            },
            "requestState": request_state,
        },
    )


async def mcp_handler(request: web.Request) -> web.Response:
    """Handle the minimal MCP discovery and multi-round-trip guard flow."""
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response(_jsonrpc_error(None, -32700, "Invalid JSON"), status=400)
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return web.json_response(_jsonrpc_error(payload.get("id") if isinstance(payload, dict) else None, -32600, "Invalid JSON-RPC request"), status=400)
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    if method == "initialize":
        requested = str(params.get("protocolVersion") or _LEGACY_PROTOCOL_VERSION)
        version = _PROTOCOL_VERSION if requested == _PROTOCOL_VERSION else _LEGACY_PROTOCOL_VERSION
        return web.json_response(
            _jsonrpc(request_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tusker-ai-gateway", "version": "0.1.0"},
            })
        )
    if method == "notifications/initialized":
        return web.Response(status=202)
    if method == "tools/list":
        return web.json_response(_jsonrpc(request_id, {
            "tools": [{
                "name": _GUARD_TOOL,
                "description": "Obtain user approval before executing any high-impact tool action.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tool_name": {"type": "string"},
                        "tool_arguments": {"type": "object"},
                    },
                    "required": ["tool_name"],
                },
            }]
        }))
    if method == "tools/call":
        if not isinstance(params, dict):
            return web.json_response(_jsonrpc_error(request_id, -32602, "Invalid tools/call params"), status=400)
        if params.get("name") != _GUARD_TOOL:
            return web.json_response(_jsonrpc_error(request_id, -32602, "Unknown tool"), status=400)
        return web.json_response(_guard_call(request_id, params))
    return web.json_response(_jsonrpc_error(request_id, -32601, "Method not found"), status=404)
