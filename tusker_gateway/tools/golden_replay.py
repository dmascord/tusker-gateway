"""Replay the captured golden fixtures through the gateway.

Runbook Phase 2.4: every captured fixture must run through the gateway with
one upstream *and* with failover across two different providers, in both
streaming and non-streaming form.

The committed fixtures are sanitized — every prompt and tool-argument string
is ``<redacted>`` — so this is a **shape** test, not a content test. It proves
the gateway accepts the framing a real client actually sent: the message role
ordering, the assistant ``tool_calls`` block, the ``tool_call_id`` linkage and
the client-specific body keys (``store``, ``stream_options``,
``max_completion_tokens``) that a hand-written scenario would omit.

Rebuilding a sendable request from a sanitized fixture is deliberate: the
reduced tool schemas are re-expanded to valid JSON Schema and the redacted
argument strings are replaced with ``{}``. Nothing private is reintroduced.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "golden"

# Body keys a real client sent that are part of the wire contract: the gateway
# must tolerate them. Values are carried verbatim from the fixture (they are
# booleans/objects, never private text).
CARRIED_BODY_KEYS = ("store", "stream_options", "max_completion_tokens")


def load_golden_requests() -> list[tuple[str, dict[str, Any]]]:
    """Every captured request, as ``(label, captured-record)`` pairs."""
    out: list[tuple[str, dict[str, Any]]] = []
    if not GOLDEN_DIR.exists():
        return out
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except ValueError:
            continue
        for index, captured in enumerate(record.get("requests") or []):
            out.append((f"{path.stem}[{index}]", captured))
    return out


def _rebuild_messages(captured: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn sanitized messages back into a sendable Chat Completions list."""
    messages: list[dict[str, Any]] = []
    for message in captured.get("messages") or []:
        if not isinstance(message, dict):
            continue
        out: dict[str, Any] = {"role": message.get("role")}
        if message.get("content") is not None:
            out["content"] = message["content"]
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls:
            out["tool_calls"] = [
                {
                    "id": call.get("id") or "call_golden",
                    "type": call.get("type") or "function",
                    "function": {
                        "name": (call.get("function") or {}).get("name"),
                        # Redacted arguments are not valid JSON; the shape is
                        # the contract here, so send an empty object.
                        "arguments": "{}",
                    },
                }
                for call in calls
                if isinstance(call, dict)
            ]
        if message.get("tool_call_id"):
            out["tool_call_id"] = message["tool_call_id"]
        messages.append(out)
    return messages


def _rebuild_tools(captured: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-expand the reduced tool schemas into valid JSON Schema."""
    tools: list[dict[str, Any]] = []
    for tool in captured.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        params = fn.get("parameters") or {}
        names = [n for n in (params.get("properties") or []) if isinstance(n, str)]
        tools.append({
            "type": tool.get("type") or "function",
            "function": {
                "name": fn.get("name"),
                "parameters": {
                    "type": "object",
                    "properties": {name: {"type": "string"} for name in names},
                    "required": [
                        r for r in (params.get("required") or [])
                        if r in names
                    ],
                },
            },
        })
    return tools


def rebuild_body(captured: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    """A sendable request that reproduces the captured shape."""
    body: dict[str, Any] = {
        "model": "hermes-code",
        "messages": _rebuild_messages(captured),
        "stream": stream,
    }
    tools = _rebuild_tools(captured)
    if tools:
        body["tools"] = tools
    for key in CARRIED_BODY_KEYS:
        if key in captured:
            body[key] = captured[key]
    return body


def _reply_body(stream: bool) -> Any:
    if stream:
        from tusker_gateway.tools.protocol_replay_matrix import _chunk
        return [
            _chunk({"content": "golden ok"}),
            _chunk({}, "stop"),
            b"data: [DONE]\n\n",
        ]
    return {
        "id": "chatcmpl-golden", "object": "chat.completion",
        "model": "sim-model",
        "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "golden ok"},
        }],
    }


def _shape_failures(
    status: int, text: str, *, stream: bool,
) -> list[str]:
    """The gateway must accept the shape and answer in the same mode."""
    from tusker_gateway.tools.protocol_replay_matrix import (
        collect_stream_frames, stream_text,
    )

    failures: list[str] = []
    if status != 200:
        failures.append(f"status {status}: {text[:200]}")
        return failures
    if stream:
        frames = collect_stream_frames(text)
        if "golden ok" not in stream_text(frames):
            failures.append("streamed reply missing the scripted content")
        if not any(f.get("__done__") for f in frames):
            failures.append("streamed reply missing the terminal [DONE]")
    else:
        try:
            body = json.loads(text)
        except ValueError:
            failures.append("non-streaming reply was not JSON")
            return failures
        content = (
            ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
        )
        if content != "golden ok":
            failures.append(f"non-streaming reply content {content!r}")
    return failures


async def _run_one(
    label: str, captured: dict[str, Any], *, stream: bool, failover: bool,
) -> dict[str, Any]:
    from aiohttp.test_utils import TestClient, TestServer

    from tusker_gateway.tools.protocol_replay_matrix import (
        Scenario, Simulator, _AUTH_HEADERS, _build_app, _reset_failure_state,
        gateway_config,
    )

    _reset_failure_state()
    scenario = Scenario(
        name=f"golden_{label}", stream=stream,
        frames=_reply_body(True) if stream else [],
        json_body=None if stream else _reply_body(False),
    )
    if failover:
        alpha = Scenario(name="golden_alpha", stream=False, status=503,
                         json_body={"error": {"message": "down"}})
        behaviour = {"alpha": alpha, "beta": scenario}
        providers = ["alpha", "beta"]
    else:
        behaviour = {"alpha": scenario}
        providers = ["alpha"]
    simulator = Simulator(behaviour)
    await simulator.start()
    config = gateway_config(
        simulator.base_url, providers,
        pool_models=[{"provider": p, "model": "sim-model"} for p in providers],
    )
    failures: list[str] = []
    status = 0
    try:
        async with aiohttp.ClientSession() as session:
            app = _build_app(config, session=session)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/v1/chat/completions",
                    json=rebuild_body(captured, stream=stream),
                    headers=_AUTH_HEADERS,
                )
                status = resp.status
                text = await resp.text()
            finally:
                await client.close()
        failures = _shape_failures(status, text, stream=stream)
        if failover:
            # The failing provider must have been tried, then the healthy one.
            if not simulator.saw_requests.get("alpha"):
                failures.append("alpha (failing) was never attempted")
            if not simulator.saw_requests.get("beta"):
                failures.append("beta (healthy) was never attempted")
    finally:
        await simulator.stop()
    mode = "failover" if failover else ("stream" if stream else "nonstream")
    return {
        "scenario": f"golden/{label}/{mode}",
        "provider": "alpha->beta" if failover else "alpha",
        "status": status, "passed": not failures, "failures": failures,
    }


async def golden_replay_rows() -> list[dict[str, Any]]:
    """Replay every captured request: stream, non-stream, and with failover."""
    rows: list[dict[str, Any]] = []
    for label, captured in load_golden_requests():
        rows.append(await _run_one(label, captured, stream=True, failover=False))
        rows.append(await _run_one(label, captured, stream=False, failover=False))
        rows.append(await _run_one(label, captured, stream=True, failover=True))
    return rows
