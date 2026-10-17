"""Deterministic protocol replay matrix.

Drives the gateway's *real* HTTP/SSE surface against a scripted provider
simulator (a live aiohttp upstream), so every scenario exercises the same
code path a client would: request normalization, stream normalization,
tool-call assembly, failover and the Responses/Anthropic adapters.

No live upstream, credential or network egress is used: the simulator is
bound to a loopback port and the gateway config points at it. Fixtures are
sanitized by construction — they contain only protocol structure
(field names, ordering, finish reasons), never real prompts, reasoning
text or credentials.

Usage::

    python3 -m tusker_gateway.tools.protocol_replay_matrix --json
    python3 -m tusker_gateway.tools.protocol_replay_matrix            # table
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web

# ─── provider simulator ──────────────────────────────────────────────────────

def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    return _sse({
        "id": "chatcmpl-sim", "object": "chat.completion.chunk",
        "model": "sim-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    })


ROLE = {"role": "assistant"}


@dataclass
class Scenario:
    """One scripted upstream behaviour plus the client-observable contract."""

    name: str
    # Frames (streaming) or a JSON body (nonstreaming) the simulator returns.
    frames: list[bytes] = field(default_factory=list)
    json_body: dict[str, Any] | None = None
    stream: bool = False
    # HTTP status override (for 5xx/429 scenarios).
    status: int = 200
    # Close the connection before [DONE] (mid-stream drop).
    drop_before_done: bool = False
    # Seconds to wait between streamed frames. Used by the cancellation
    # check, which needs the client to disconnect while the upstream is
    # still producing.
    frame_delay: float = 0.0
    # Request body overrides for scenarios that need a specific message
    # shape (tool-result continuation, multi-turn, etc.).
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    # Asserts run against the gateway's client-visible response.
    expects: dict[str, Any] = field(default_factory=dict)

# ─── scenarios ──────────────────────────────────────────────────────────────

def _tool_call_frame(index: int, call_id: str, name: str, args: str,
                     finish: str | None = None) -> bytes:
    return _chunk({
        "tool_calls": [{
            "index": index, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": args},
        }],
    }, finish)


def _tool_json(call_id: str, name: str, args: dict[str, Any],
               finish: str = "tool_calls") -> dict[str, Any]:
    return {
        "id": "chatcmpl-sim", "object": "chat.completion", "model": "sim-model",
        "choices": [{
            "index": 0, "finish_reason": finish,
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            },
        }],
    }


def scenarios() -> list[Scenario]:
    return [
        Scenario(
            name="plain_text_stream",
            stream=True,
            frames=[
                _chunk(ROLE), _chunk({"content": "hello world"}),
                _chunk({}, "stop"), b"data: [DONE]\n\n",
            ],
            expects={"content_includes": "hello world", "finish": "stop",
                     "done": True, "no_reasoning_in_content": True},
        ),
        Scenario(
            name="reasoning_only_stream",
            stream=True,
            frames=[
                _chunk({"reasoning_content": "think", "content": None}),
                _chunk({"reasoning_content": " more", "content": None}, "stop"),
                b"data: [DONE]\n\n",
            ],
            expects={"reasoning_present": "think",
                     "reasoning_not_in_content": "think",
                     "finish": "stop", "done": True},
        ),
        Scenario(
            name="reasoning_then_content_stream",
            stream=True,
            frames=[
                _chunk({"reasoning_content": "hmm", "content": None}),
                _chunk({"content": "answer"}),
                _chunk({}, "stop"), b"data: [DONE]\n\n",
            ],
            expects={"reasoning_present": "hmm", "content_includes": "answer",
                     "reasoning_not_in_content": "hmm", "done": True},
        ),
        Scenario(
            name="reasoning_alias_reasoning_field",
            stream=True,
            frames=[
                _chunk({"reasoning": "alias-thought"}),
                _chunk({"content": "done"}),
                _chunk({}, "stop"), b"data: [DONE]\n\n",
            ],
            expects={"reasoning_present": "alias-thought",
                     "reasoning_not_in_content": "alias-thought"},
        ),
        Scenario(
            name="native_tool_call_stream",
            stream=True,
            frames=[
                _chunk(ROLE),
                _tool_call_frame(0, "call_a", "bash", '{"command":"ls"}'),
                _chunk({}, "tool_calls"), b"data: [DONE]\n\n",
            ],
            expects={"tool_call_name": "bash", "tool_call_id": "call_a",
                     "tool_args_json": {"command": "ls"},
                     "finish": "tool_calls", "done": True},
        ),
        Scenario(
            name="split_json_arguments_stream",
            stream=True,
            frames=[
                _chunk(ROLE),
                _tool_call_frame(0, "call_a", "bash", '{"comm'),
                _tool_call_frame(0, None, None, 'and":"ls"}'),
                _chunk({}, "tool_calls"), b"data: [DONE]\n\n",
            ],
            expects={"tool_call_name": "bash", "tool_call_id": "call_a",
                     "tool_args_json": {"command": "ls"}},
        ),
        Scenario(
            name="parallel_tool_calls_stream",
            stream=True,
            frames=[
                _chunk(ROLE),
                _tool_call_frame(0, "call_a", "bash", '{"command":"ls"}'),
                _tool_call_frame(1, "call_b", "read", '{"path":"x"}'),
                _chunk({}, "tool_calls"), b"data: [DONE]\n\n",
            ],
            expects={"tool_call_count": 2, "finish": "tool_calls"},
        ),
        Scenario(
            name="missing_finish_reason_stream",
            stream=True,
            frames=[
                _chunk({"content": "partial"}),
                b"data: [DONE]\n\n",
            ],
            expects={"content_includes": "partial", "done": True},
        ),
        Scenario(
            name="duplicate_dsml_markup_in_content_stream",
            stream=True,
            frames=[
                _chunk({"content": "<|tool_call|>{\"name\":\"bash\"}<|/tool_call|>"}),
                _chunk({}, "stop"), b"data: [DONE]\n\n",
            ],
            expects={"markup_absent": ["<|tool_call|>", "<|/tool_call|>"]},
        ),
        Scenario(
            name="native_tool_call_nonstream",
            json_body=_tool_json("call_a", "bash", {"command": "ls"}),
            expects={"tool_call_name": "bash", "tool_call_id": "call_a",
                     "tool_args_json": {"command": "ls"}, "finish": "tool_calls"},
        ),
        Scenario(
            name="plain_text_nonstream",
            json_body={
                "id": "chatcmpl-sim", "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": "hello"}}],
            },
            expects={"content_includes": "hello", "finish": "stop"},
        ),
        Scenario(
            name="upstream_500",
            stream=False,
            status=500,
            json_body={"error": {"message": "boom", "type": "server_error"}},
            expects={"upstream_failure": True},
        ),
        Scenario(
            name="upstream_429",
            stream=False,
            status=429,
            json_body={"error": {"message": "slow down", "type": "rate_limit"}},
            expects={"upstream_failure": True},
        ),
        Scenario(
            name="mid_stream_close",
            stream=True,
            drop_before_done=True,
            frames=[
                _chunk({"content": "partial"}),  # no finish, no [DONE]
            ],
            expects={"mid_stream_close": "partial"},
        ),
        Scenario(
            name="empty_content_stream",
            stream=True,
            frames=[
                _chunk(ROLE),
                _chunk({"content": ""}),
                _chunk({}, "stop"),
                b"data: [DONE]\n\n",
            ],
            expects={"finish": "stop", "done": True},
        ),
        Scenario(
            name="malformed_sse_frame",
            # Garbage between valid frames must not crash the stream loop
            # or be parsed as a tool call. The valid frame still wins.
            stream=True,
            frames=[
                _chunk(ROLE),
                b"data: not-json at all\n\n",
                _chunk({"content": "ok"}),
                _chunk({}, "stop"),
                b"data: [DONE]\n\n",
            ],
            expects={"content_includes": "ok", "finish": "stop", "done": True},
        ),
        Scenario(
            name="tool_result_continuation_nonstream",
            # The client has already run a tool call and is sending the
            # result back so the model can finish the turn. The gateway
            # must accept the prior assistant.tool_calls + tool role
            # shape without rejecting the request, and surface the
            # model's reply.
            messages=[
                {"role": "user", "content": "What is 6 times 7?"},
                {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_a", "type": "function",
                        "function": {"name": "calc", "arguments": '{"expr":"6*7"}'},
                    }],
                },
                {"role": "tool", "tool_call_id": "call_a", "content": "42"},
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "calc",
                    "description": "Compute an expression",
                    "parameters": {
                        "type": "object",
                        "properties": {"expr": {"type": "string"}},
                    },
                },
            }],
            json_body={
                "id": "chatcmpl-sim", "object": "chat.completion",
                "choices": [{
                    "index": 0, "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "after the tool call, the answer is 42",
                    },
                }],
            },
            expects={"content_includes": "answer is 42", "finish": "stop"},
        ),
    ]


# ─── simulator server ───────────────────────────────────────────────────────

class Simulator:
    """A scripted OpenAI-compatible upstream.

    ``behaviour`` maps provider name → the scenario that provider serves.
    ``saw_requests`` records each request body per provider so the matrix can
    assert what the gateway actually sent upstream.
    """

    def __init__(self, behaviour: dict[str, Scenario]):
        self.behaviour = behaviour
        self.saw_requests: dict[str, list[dict[str, Any]]] = {p: [] for p in behaviour}
        self._app = web.Application()
        self._app.router.add_post("/{provider}/v1/chat/completions", self._chat)
        self._runner: web.AppRunner | None = None
        self.base_url: str | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        provider = request.match_info["provider"]
        body = await request.json()
        self.saw_requests.setdefault(provider, []).append(body)
        scenario = self.behaviour[provider]
        if scenario.status >= 400:
            return web.json_response(scenario.json_body or {}, status=scenario.status)
        if not scenario.stream:
            return web.json_response(scenario.json_body or {})
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
        })
        await resp.prepare(request)
        for frame in scenario.frames:
            if scenario.frame_delay:
                # The cancellation check disconnects mid-stream; the delay
                # keeps the upstream producing so the teardown path is the
                # one under test rather than a completed stream.
                await asyncio.sleep(scenario.frame_delay)
            await resp.write(frame)
        if scenario.drop_before_done:
            # Peer reset: the frames above are already flushed by
            # ``resp.write``. Abort the transport without a chunked
            # terminator so the reader raises instead of observing a
            # graceful EOF.
            request.transport.abort()
            return resp
        return resp


def gateway_config(simulator_url: str, providers: list[str],
                   pool_models: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Point every provider at the simulator; keys are dummy test values."""
    from tusker_gateway.config import PoolConfig

    return {
        "api_keys": ["sk-test"],
        "providers": {
            p: {
                "base_url": f"{simulator_url}/{p}",
                "chat_path": "/v1/chat/completions",
                "auth_type": "bearer",
            }
            for p in providers
        },
        "provider_api_keys": {p: "sk-test" for p in providers},
        "excluded_providers": [],
        "disabled_providers": [],
        "quality_db_path": os.path.join(tempfile.mkdtemp(prefix="replay-quality-"), "quality.db"),
        "pools": {
            "code": PoolConfig(
                name="code",
                models=pool_models or [
                    {"provider": provider, "model": "sim-model"}
                    for provider in providers
                ],
            ),
        },
        "model_aliases": {"hermes-code": "code"},
    }


# ─── contract assertions ────────────────────────────────────────────────────

def collect_stream_frames(text: str) -> list[dict[str, Any]]:
    frames = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):].strip()
        if payload == "[DONE]":
            frames.append({"__done__": True})
            continue
        try:
            frames.append(json.loads(payload))
        except ValueError:
            continue
    return frames


def stream_text(frames: list[dict[str, Any]]) -> str:
    parts = []
    for frame in frames:
        for choice in frame.get("choices", []) or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                parts.append(delta["content"])
    return "".join(parts)


def stream_reasoning(frames: list[dict[str, Any]]) -> str:
    parts = []
    for frame in frames:
        for choice in frame.get("choices", []) or []:
            delta = choice.get("delta") or {}
            for key in ("reasoning_content", "reasoning", "reasoning_text"):
                if isinstance(delta.get(key), str):
                    parts.append(delta[key])
    return "".join(parts)


def assembled_tool_calls(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reassemble streamed tool_call deltas the way a client would."""
    calls: dict[int, dict[str, Any]] = {}
    for frame in frames:
        for choice in frame.get("choices", []) or []:
            delta = choice.get("delta") or {}
            for fn in delta.get("tool_calls") or []:
                idx = fn.get("index") or 0
                entry = calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                if fn.get("id"):
                    entry["id"] = fn["id"]
                fn_def = fn.get("function") or {}
                if fn_def.get("name"):
                    entry["name"] += fn_def["name"]
                if isinstance(fn_def.get("arguments"), str):
                    entry["arguments"] += fn_def["arguments"]
    return [calls[k] for k in sorted(calls)]


def stream_finish(frames: list[dict[str, Any]]) -> str | None:
    for frame in frames:
        for choice in frame.get("choices", []) or []:
            if choice.get("finish_reason"):
                return choice["finish_reason"]
    return None


def evaluate(scenario: Scenario, response: dict[str, Any]) -> list[str]:
    """Return the list of contract failures (empty == pass)."""
    exp = scenario.expects
    failures: list[str] = []
    status = response.get("status")
    if exp.get("upstream_failure"):
        # Any non-2xx is an acceptable, non-crashing failure surface; a 2xx
        # would mean the gateway masked an upstream error.
        if 200 <= status < 300:
            failures.append(f"upstream error surfaced as {status}")
        return failures
    if exp.get("mid_stream_close"):
        # Documented contract (tests/test_fault_scenarios.py): partial content
        # is surfaced, then a vLLM/OpenRouter-shape top-level `error` object
        # with code stream_closed_prematurely, then a clean [DONE] so an
        # eventsource client gets a verdict instead of hanging on read.
        text = response.get("text", "")
        frames = collect_stream_frames(text)
        if exp["mid_stream_close"] not in stream_text(frames):
            failures.append("mid-stream close dropped the partial content")
        if not any((f.get("error") or {}).get("code") == "stream_closed_prematurely"
                   for f in frames):
            failures.append("mid-stream close omitted the stream_closed_prematurely error frame")
        if not any(f.get("__done__") for f in frames):
            failures.append("mid-stream close omitted the terminal [DONE]")
        return failures
    if status != 200:
        failures.append(f"expected 200, got {status}")
        return failures
    if scenario.stream:
        frames = collect_stream_frames(response.get("text", ""))
        content = stream_text(frames)
        reasoning = stream_reasoning(frames)
        if "content_includes" in exp and exp["content_includes"] not in content:
            failures.append(f"content missing {exp['content_includes']!r}")
        if "reasoning_present" in exp and exp["reasoning_present"] not in reasoning:
            failures.append(f"reasoning channel missing {exp['reasoning_present']!r}")
        if exp.get("reasoning_not_in_content") and exp["reasoning_not_in_content"] in content:
            failures.append("reasoning leaked into content")
        if exp.get("no_reasoning_in_content") and reasoning and reasoning in content:
            failures.append("reasoning leaked into content")
        if "finish" in exp and stream_finish(frames) != exp["finish"]:
            failures.append(f"finish_reason {stream_finish(frames)!r} != {exp['finish']!r}")
        if exp.get("done") and not any(f.get("__done__") for f in frames):
            failures.append("missing [DONE]")
        calls = assembled_tool_calls(frames)
        if "tool_call_name" in exp:
            if len(calls) != 1 or calls[0]["name"] != exp["tool_call_name"]:
                failures.append(f"tool name mismatch: {calls}")
        if "tool_call_id" in exp:
            if len(calls) != 1 or calls[0]["id"] != exp["tool_call_id"]:
                failures.append(f"tool id mismatch: {calls}")
        if "tool_args_json" in exp:
            try:
                parsed = json.loads(calls[0]["arguments"]) if len(calls) == 1 else None
            except ValueError:
                parsed = None
            if parsed != exp["tool_args_json"]:
                failures.append(f"tool args mismatch: {parsed!r}")
        if "tool_call_count" in exp and len(calls) != exp["tool_call_count"]:
            failures.append(f"tool call count {len(calls)} != {exp['tool_call_count']}")
        for markup in exp.get("markup_absent", []):
            if markup in content or markup in reasoning:
                failures.append(f"markup {markup!r} leaked")
    else:
        body = response.get("json") or {}
        choices = body.get("choices") or [{}]
        message = (choices[0].get("message") or {})
        content = message.get("content") or ""
        if "content_includes" in exp and exp["content_includes"] not in content:
            failures.append(f"content missing {exp['content_includes']!r}")
        if "finish" in exp and choices[0].get("finish_reason") != exp["finish"]:
            failures.append("finish_reason mismatch")
        calls = message.get("tool_calls") or []
        if "tool_call_name" in exp:
            if len(calls) != 1 or calls[0]["function"]["name"] != exp["tool_call_name"]:
                failures.append(f"tool name mismatch: {calls}")
        if "tool_call_id" in exp:
            if len(calls) != 1 or calls[0]["id"] != exp["tool_call_id"]:
                failures.append(f"tool id mismatch: {calls}")
        if "tool_args_json" in exp:
            try:
                parsed = json.loads(calls[0]["function"]["arguments"]) if len(calls) == 1 else None
            except ValueError:
                parsed = None
            if parsed != exp["tool_args_json"]:
                failures.append(f"tool args mismatch: {parsed!r}")
    return failures
_AUTH_HEADERS = {"Authorization": "Bearer sk-test"}


# ─── runner ─────────────────────────────────────────────────────────────────

async def _run_scenario(
    client: Any, scenario: Scenario, provider: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "hermes-code",
        "messages": scenario.messages or [{"role": "user", "content": "ping"}],
        "stream": scenario.stream,
    }
    if scenario.tools:
        payload["tools"] = scenario.tools
    resp = await client.post("/v1/chat/completions", json=payload,
                             headers=_AUTH_HEADERS)
    text = await resp.text()
    result: dict[str, Any] = {"status": resp.status, "text": text}
    if not scenario.stream:
        try:
            result["json"] = json.loads(text)
        except ValueError:
            result["json"] = None
    failures = evaluate(scenario, result)
    return {
        "scenario": scenario.name,
        "provider": provider,
        "status": result["status"],
        "passed": not failures,
        "failures": failures,
    }


def _reset_failure_state() -> None:
    """Clear process-global cooldown/capacity state between scenarios.

    Mirrors ``tests/conftest.py::reset_cooldown_tracker``: without this a
    429/5xx scenario poisons the next scenario's pool selection, which is
    harness cross-talk rather than a gateway defect.
    """
    from tusker_gateway.cooldown import PERMANENTLY_FAILED_MODELS, global_tracker
    from tusker_gateway.provider_usage import capacity_controller

    tracker = global_tracker()
    tracker._cooldowns.clear()
    tracker._provider_default.clear()
    tracker._group_cooldowns.clear()
    tracker._recent_failures.clear()
    tracker._global = None
    PERMANENTLY_FAILED_MODELS.clear()
    capacity_controller().reset()


async def run_matrix(*, failover: bool = True) -> dict[str, Any]:
    """Run every scenario, once single-upstream and once with failover."""
    from aiohttp.test_utils import TestClient, TestServer

    results: list[dict[str, Any]] = []

    # Single-upstream pass: provider "alpha" serves each scenario in turn.
    for scenario in scenarios():
        _reset_failure_state()
        simulator = Simulator({"alpha": scenario})
        await simulator.start()
        config = gateway_config(simulator.base_url, ["alpha"])
        async with aiohttp.ClientSession() as session:
            app = _build_app(config, session=session)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                results.append(await _run_scenario(client, scenario, "alpha"))
            finally:
                await client.close()
        await simulator.stop()

    # Operator diagnostics: the access log must carry the per-attempt
    # protocol fields for a routed request. Asserted here (not just
    # documented) because the fields are the operator-facing surface
    # the runbook requires.
    results.append(await _check_access_log_diagnostics())
    results.append(await _check_responses_pairing())
    # Runbook 2.3 also lists cancellation and retry. Both are lifecycle
    # contracts rather than single-response shapes, so they are driven
    # directly instead of through the scenario table.
    results.append(await _check_client_cancellation())
    results.append(await _check_transient_retry_recovery())

    # Runbook Phase 2.4: the captured golden fixtures (real OpenCode and
    # OpenAI-SDK requests) must replay through the gateway in both modes and
    # under cross-provider failover — not just sit in a directory.
    from tusker_gateway.tools.golden_replay import golden_replay_rows
    results.extend(await golden_replay_rows())

    if failover:
        _reset_failure_state()
        results.extend(await _run_failover_scenarios())
    return {
        "scenarios": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": [r for r in results if not r["passed"]],
        "results": results,
    }


async def _check_access_log_diagnostics() -> dict[str, Any]:
    """Assert per-attempt protocol diagnostics reach the access log.

    Drives one routed request through the same app factory the scenarios
    use, capturing the ``tusker_gateway.access`` records, and requires
    ``protocol_family`` / ``protocol_confidence`` to be present. These
    fields are the operator-facing surface for the compatibility
    resolver; a refactor that drops them should fail the matrix rather
    than silently losing diagnostics.
    """
    import logging

    from aiohttp.test_utils import TestClient, TestServer

    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    logger = logging.getLogger("tusker_gateway.access")
    handler = _Capture()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        _reset_failure_state()
        scenario = Scenario(
            name="diagnostics", stream=False,
            json_body={
                "id": "chatcmpl-sim", "object": "chat.completion",
                "choices": [{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }],
            },
        )
        simulator = Simulator({"alpha": scenario})
        await simulator.start()
        config = gateway_config(simulator.base_url, ["alpha"])
        async with aiohttp.ClientSession() as session:
            app = _build_app(config, session=session)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-code",
                          "messages": [{"role": "user", "content": "ping"}]},
                    headers=_AUTH_HEADERS,
                )
                await resp.text()
                # The direct (provider-prefixed) route bypasses pool
                # selection, so it asserts the same diagnostics separately:
                # an operator reading a direct-route record must see the
                # endpoint and adapter just as on a pooled attempt.
                direct = await client.post(
                    "/v1/chat/completions",
                    json={"model": "alpha/sim-model",
                          "messages": [{"role": "user", "content": "ping"}]},
                    headers=_AUTH_HEADERS,
                )
                await direct.text()
            finally:
                await client.close()
        await simulator.stop()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    failures: list[str] = []
    routed = []
    for message in captured:
        try:
            record = json.loads(message)
        except ValueError:
            continue
        if record.get("provider"):
            routed.append(record)
    if not routed:
        failures.append("no routed access-log record was emitted")
    else:
        # Both routes must carry the full diagnostics set: the pooled
        # (``hermes-code``) attempt and the direct (``alpha/sim-model``)
        # attempt. Asserting every routed record pins both paths.
        for record in routed:
            label = record.get("route_kind") or "unknown"
            for field in (
                "protocol_family", "protocol_confidence", "endpoint_id",
                "evidence_version", "selected_adapter",
            ):
                if not record.get(field):
                    failures.append(f"access log missing {field} (route {label})")
            # The runbook forbids prompt/reasoning/tool-argument text in
            # telemetry: assert the record carries none of the request content.
            blob = json.dumps(record)
            if "ping" in blob:
                failures.append(f"access log leaked prompt content (route {label})")
            # No secret material may leak alongside the diagnostics.
            if "sk-test" in blob or "Authorization" in blob:
                failures.append(f"access log leaked credential material (route {label})")
        if {r.get("route_kind") for r in routed} != {"pool", "direct"}:
            failures.append("expected exactly one pooled and one direct record")
    return {
        "scenario": "operator_diagnostics", "provider": "alpha",
        "status": 200, "passed": not failures, "failures": failures,
    }

async def _check_responses_pairing() -> dict[str, Any]:
    """Drive the Responses route end-to-end through a call/output pair.

    The gateway registers ``/v1/responses`` but the chat-shaped scenarios
    never exercise it. This sends a two-item ``input`` — an assistant
    ``function_call`` followed by its ``function_call_output`` — and
    asserts the gateway translates the pair into a chat turn the upstream
    accepts, then re-shapes the reply into a Responses envelope. That
    translation is the pairing contract OpenCode/Cline clients depend on.
    """
    from aiohttp.test_utils import TestClient, TestServer

    scenario = Scenario(
        name="responses_pairing", stream=False,
        json_body={
            "id": "chatcmpl-sim", "object": "chat.completion",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "paired ok"},
            }],
        },
    )
    simulator = Simulator({"alpha": scenario})
    await simulator.start()
    config = gateway_config(simulator.base_url, ["alpha"])
    failures: list[str] = []
    body: dict[str, Any] = {}
    try:
        async with aiohttp.ClientSession() as session:
            app = _build_app(config, session=session)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/v1/responses",
                    json={
                        "model": "hermes-code",
                        "input": [
                            {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": "run calc"}]},
                            {"type": "function_call", "name": "calc",
                             "call_id": "call_pair", "arguments": '{"expr":"6*7"}'},
                            {"type": "function_call_output", "call_id": "call_pair",
                             "output": "42"},
                        ],
                    },
                    headers=_AUTH_HEADERS,
                )
                text = await resp.text()
            finally:
                await client.close()
        if resp.status != 200:
            failures.append(f"status {resp.status}: {text[:200]}")
        else:
            try:
                body = json.loads(text)
            except ValueError:
                failures.append("non-JSON Responses body")
        if body:
            if body.get("object") != "response":
                failures.append(f"object {body.get('object')!r} != 'response'")
            if body.get("status") != "completed":
                failures.append(f"status {body.get('status')!r} != 'completed'")
            texts = [
                part.get("text")
                for item in body.get("output", [])
                for part in item.get("content", []) or []
                if part.get("type") == "output_text"
            ]
            if "paired ok" not in texts:
                failures.append(f"output_text missing reply: {texts!r}")
        # The upstream must have received the pair as a chat turn:
        # assistant.tool_calls + a tool-role result, not two user messages.
        sent = simulator.saw_requests.get("alpha") or []
        if not sent:
            failures.append("upstream saw no request")
        else:
            messages = sent[-1].get("messages") or []
            roles = [m.get("role") for m in messages]
            if "tool" not in roles:
                failures.append(f"pairing lost: roles {roles!r}")
            assistant = [m for m in messages if m.get("role") == "assistant"]
            if not any(m.get("tool_calls") for m in assistant):
                failures.append("assistant.tool_calls not reconstructed upstream")
    finally:
        await simulator.stop()
    return {
        "scenario": "responses_pairing", "provider": "alpha",
        "status": 200, "passed": not failures, "failures": failures,
    }


async def _run_failover_scenarios() -> list[dict[str, Any]]:
    """Every success-path fixture, replayed across two providers.

    Runbook Phase 2.4 requires each fixture to be exercised with one
    upstream *and* with failover across two different providers. Alpha
    fails at the request level (503); beta serves the fixture's own
    contract, so the client-visible result must satisfy the same
    expectations the single-upstream pass asserts — plus both providers
    must have been attempted.

    Failure-shaped fixtures (4xx/5xx responses, mid-stream close) are
    excluded: for those the ``expects`` describe the *failure* surface,
    so a clean fallback would be the wrong assertion. Their failover
    behaviour is covered by ``tests/test_fault_scenarios.py``.
    """
    from aiohttp.test_utils import TestClient, TestServer

    results: list[dict[str, Any]] = []
    for scenario in scenarios():
        if scenario.status != 200 or scenario.drop_before_done:
            continue
        _reset_failure_state()
        alpha = Scenario(name="failover_source", stream=False, status=503,
                         json_body={"error": {"message": "down"}})
        simulator = Simulator({"alpha": alpha, "beta": scenario})
        await simulator.start()
        config = gateway_config(
            simulator.base_url, ["alpha", "beta"],
            pool_models=[
                {"provider": "alpha", "model": "sim-model"},
                {"provider": "beta", "model": "sim-model"},
            ],
        )
        try:
            async with aiohttp.ClientSession() as session:
                app = _build_app(config, session=session)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    payload: dict[str, Any] = {
                        "model": "hermes-code",
                        "messages": scenario.messages
                        or [{"role": "user", "content": "ping"}],
                        "stream": scenario.stream,
                    }
                    if scenario.tools:
                        payload["tools"] = scenario.tools
                    resp = await client.post("/v1/chat/completions",
                                             json=payload,
                                             headers=_AUTH_HEADERS)
                    text = await resp.text()
                finally:
                    await client.close()
            result: dict[str, Any] = {"status": resp.status, "text": text}
            if not scenario.stream:
                try:
                    result["json"] = json.loads(text)
                except ValueError:
                    result["json"] = None
            failures = evaluate(scenario, result)
            if not simulator.saw_requests.get("alpha"):
                failures.append("alpha was never attempted")
            if not simulator.saw_requests.get("beta"):
                failures.append("beta was never attempted")
        finally:
            await simulator.stop()
        results.append({
            "scenario": f"failover/{scenario.name}", "provider": "alpha->beta",
            "status": result["status"], "passed": not failures,
            "failures": failures,
        })
    return results


async def _check_client_cancellation() -> dict[str, Any]:
    """A client that disconnects mid-stream must not corrupt the gateway.

    Runbook 2.3 lists cancellation as a required fixture. The contract is
    the teardown path: no hang, no 5xx for the *next* request, and the
    stream loop exits rather than spinning on a dead peer. The upstream
    keeps producing (``frame_delay``) so the disconnect happens mid-flight.
    """
    from aiohttp.test_utils import TestClient, TestServer

    _reset_failure_state()
    scenario = Scenario(
        name="cancel_source", stream=True, frame_delay=0.05,
        frames=[_chunk({"content": f"chunk-{i}"}) for i in range(40)],
    )
    followup = Scenario(
        name="cancel_followup", stream=False,
        json_body={
            "id": "chatcmpl-sim", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "still alive"}}],
        },
    )
    simulator = Simulator({"alpha": scenario})
    await simulator.start()
    config = gateway_config(simulator.base_url, ["alpha"])
    failures: list[str] = []
    try:
        async with aiohttp.ClientSession() as session:
            app = _build_app(config, session=session)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-code",
                          "messages": [{"role": "user", "content": "ping"}],
                          "stream": True},
                    headers=_AUTH_HEADERS,
                )
                if resp.status != 200:
                    failures.append(f"cancel stream did not start: {resp.status}")
                else:
                    read = 0
                    async for _ in resp.content.iter_any():
                        read += 1
                        if read >= 2:
                            break
                    resp.close()
                    if read < 2:
                        failures.append("stream produced fewer frames than expected")
                # The gateway must still serve the next request: a stuck
                # stream loop or a poisoned pool would show up here.
                simulator.behaviour["alpha"] = followup
                after = await client.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-code",
                          "messages": [{"role": "user", "content": "again"}]},
                    headers=_AUTH_HEADERS,
                )
                text = await after.text()
                if after.status != 200:
                    failures.append(f"post-cancel request failed: {after.status}")
                elif "still alive" not in text:
                    failures.append("post-cancel request returned the wrong body")
            finally:
                await client.close()
    finally:
        await simulator.stop()
    return {
        "scenario": "client_cancellation", "provider": "alpha",
        "status": 200, "passed": not failures, "failures": failures,
    }


async def _check_transient_retry_recovery() -> dict[str, Any]:
    """A transient upstream failure must not poison the route afterwards.

    Runbook 2.3 lists retry as a required fixture. A 503 on the first
    request is expected to fail that request; what matters is that the
    *next* request reaches the upstream and succeeds rather than being
    served the cached failure or blocked by a breaker that never resets.
    """
    from aiohttp.test_utils import TestClient, TestServer

    _reset_failure_state()
    failing = Scenario(name="transient_fail", stream=False, status=503,
                       json_body={"error": {"message": "temporarily down"}})
    healthy = Scenario(
        name="transient_ok", stream=False,
        json_body={
            "id": "chatcmpl-sim", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "recovered"}}],
        },
    )
    simulator = Simulator({"alpha": failing})
    await simulator.start()
    config = gateway_config(simulator.base_url, ["alpha"])
    failures: list[str] = []
    try:
        async with aiohttp.ClientSession() as session:
            app = _build_app(config, session=session)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                first = await client.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-code",
                          "messages": [{"role": "user", "content": "ping"}]},
                    headers=_AUTH_HEADERS,
                )
                await first.text()
                if first.status < 400:
                    failures.append(
                        f"transient upstream failure was swallowed: {first.status}"
                    )
                # Heal the upstream and clear the cooldown the failure armed;
                # the contract is that recovery is possible, not that a single
                # 5xx keeps the route down forever.
                simulator.behaviour["alpha"] = healthy
                _reset_failure_state()
                second = await client.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-code",
                          "messages": [{"role": "user", "content": "ping"}]},
                    headers=_AUTH_HEADERS,
                )
                text = await second.text()
                if second.status != 200:
                    failures.append(f"recovery request failed: {second.status}")
                elif "recovered" not in text:
                    failures.append("recovery request did not reach the upstream")
                if len(simulator.saw_requests.get("alpha", [])) != 2:
                    failures.append(
                        "expected exactly two upstream requests "
                        "(first failed, second served)"
                    )
            finally:
                await client.close()
    finally:
        await simulator.stop()
    return {
        "scenario": "transient_retry_recovery", "provider": "alpha",
        "status": 200, "passed": not failures, "failures": failures,
    }


def _build_app(
    config: dict[str, Any], *, session: aiohttp.ClientSession,
) -> web.Application:
    """A gateway app wired like the test suite's, but with a live upstream
    session so the simulated provider is reached over real HTTP."""
    from aiohttp import web as _web

    from tusker_gateway.anthropic_adapter import anthropic_messages_handler
    from tusker_gateway.auth import AuthMiddleware
    from tusker_gateway.endpoints import chat_completions_handler, responses_handler

    from tusker_gateway.observability import AccessLog, attach_request_id_middleware

    app = _web.Application(client_max_size=10 * 1024 * 1024)
    app["config"] = config
    app["http_session"] = session
    app["access_log"] = AccessLog()
    auth = AuthMiddleware()

    @_web.middleware
    async def auth_middleware(request, handler):
        if request.path in ("/health", "/ready"):
            return await handler(request)
        await auth.verify(request)
        return await handler(request)

    app.middlewares.append(auth_middleware)
    # Production attaches the request-id + access-log middleware in
    # app.py; include it here so the harness exercises the real
    # observability path (including per-attempt protocol diagnostics).
    attach_request_id_middleware(app)
    app.router.add_post("/v1/chat/completions", chat_completions_handler)
    app.router.add_post("/v1/responses", responses_handler)
    app.router.add_post("/v1/messages", anthropic_messages_handler)
    return app


def _print_table(matrix: dict[str, Any]) -> None:
    print(f"{'scenario':40s} {'provider':14s} {'status':6s} result")
    for row in matrix["results"]:
        mark = "PASS" if row["passed"] else "FAIL"
        detail = "" if row["passed"] else "  " + "; ".join(row["failures"])
        print(f"{row['scenario']:40s} {row['provider']:14s} {row['status']:<6d} {mark}{detail}")
    print(f"\n{matrix['passed']}/{matrix['scenarios']} scenarios passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-failover", action="store_true")
    args = parser.parse_args(argv)
    matrix = asyncio.run(run_matrix(failover=not args.no_failover))
    if args.json:
        print(json.dumps(matrix, indent=2))
    else:
        _print_table(matrix)
    return 0 if matrix["failed"] == [] else 1


if __name__ == "__main__":
    sys.exit(main())
