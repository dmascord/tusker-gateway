"""Minimal stdio MCP server used to surface client tools to Claude Code.

The server never executes a client tool. It atomically publishes the requested
name/arguments into a private request-scoped file; the parent adapter observes
that event, stops Claude Code, and returns a normal OpenAI tool call upstream.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _response(request_id: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _load_manifest() -> list[dict[str, Any]]:
    path = os.environ.get("TUSKER_MCP_MANIFEST")
    if not path:
        return []
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return manifest if isinstance(manifest, list) else []


def _publish_call(tool: dict[str, Any], arguments: Any) -> None:
    path = os.environ.get("TUSKER_MCP_CALL_FILE")
    if not path:
        raise RuntimeError("missing request-scoped call channel")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    target = Path(path)
    payload = {
        "id": f"call_{uuid.uuid4().hex}",
        "name": tool["name"],
        "arguments": arguments,
    }
    fd, temporary = tempfile.mkstemp(prefix=".tool-call-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Linking is atomic and fails if another MCP call already won. This
        # prevents parallel tool calls from overwriting the call returned to
        # the client while the parent process is stopping Claude Code.
        os.link(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> None:
    tools = _load_manifest()
    by_mcp_name = {tool["mcp_name"]: tool for tool in tools if isinstance(tool, dict)}
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}

        # Notifications (e.g. notifications/initialized) have no response.
        if request_id is None:
            continue
        if method == "initialize":
            version = params.get("protocolVersion") or "2024-11-05"
            _response(request_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tusker-gateway", "version": "1.0.0"},
            })
        elif method == "ping":
            _response(request_id, {})
        elif method == "tools/list":
            _response(request_id, {
                "tools": [
                    {
                        "name": tool["mcp_name"],
                        "description": tool.get("description", ""),
                        "inputSchema": tool.get("input_schema", {"type": "object"}),
                    }
                    for tool in by_mcp_name.values()
                ],
            })
        elif method == "tools/call":
            tool_name = params.get("name")
            tool = by_mcp_name.get(tool_name)
            if tool is None:
                _error(request_id, -32602, "Unknown gateway tool")
                continue
            try:
                _publish_call(tool, params.get("arguments", {}))
            except (OSError, ValueError, RuntimeError):
                _error(request_id, -32603, "Could not forward gateway tool request")
                continue
            _response(request_id, {
                "content": [{
                    "type": "text",
                    "text": "Tool request was returned to the connected client. Stop and wait for its tool result.",
                }],
            })
        else:
            _error(request_id, -32601, "Method not found")


if __name__ == "__main__":
    main()
