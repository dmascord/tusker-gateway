#!/usr/bin/env python3
"""Replay an OMP-like tool/compaction flow and compare MiniMax paths.

The live mode deliberately executes no real tools. Tool calls returned by the
model are answered with synthetic successful results, which lets the harness
measure whether a model re-requests an already-completed skill read without
touching a workspace.

Examples:

    python tests/omp_minimax_harness.py --replay-only
    MINIMAX_API_KEY=... GATEWAY_API_KEY=... \
        python tests/omp_minimax_harness.py --live

The live runner compares:

* MiniMax directly: ``https://api.minimax.io/v1/chat/completions``
* Tusker Gateway pinned to the same provider/model:
  ``https://ai.tusker.net.au/v1/chat/completions`` with
  ``model=minimax::MiniMax-M3``

No prompts, tool arguments, response text, API keys, or response bodies are
printed. The gateway request carries one stable ``x-opencode-session`` value
for the whole scenario.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import aiohttp


SKILL_PATHS = (
    "skill://delivery-lifecycle",
    "skill://surgical-change-control",
)

SYSTEM_PROMPT = (
    "You are in an OMP-compatible agent loop. Use the supplied tools when "
    "needed. A successful tool result is authoritative; do not repeat a "
    "successful read for the same resource. Tool results are synthetic and "
    "safe to use."
)

INITIAL_PROMPT = (
    "Inspect the delivery state by reading the two required skill resources "
    f"({SKILL_PATHS[0]} and {SKILL_PATHS[1]}), then continue the task. "
    "Once a skill read succeeds, never request that same skill again."
)

CONTINUE_PROMPT = (
    "Continue the same agent run using the tool results already available. "
    "Do not repeat any successful skill read."
)


def tool_definitions() -> list[dict[str, Any]]:
    """Return the stable tool schema used by both live endpoints."""
    return [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a virtual resource and return its contents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run a command in the synthetic harness.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _skill_path(arguments: str) -> str | None:
    try:
        value = json.loads(arguments)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    if isinstance(path, str) and path.startswith("skill://"):
        return path.split("?", 1)[0]
    return None


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: str = ""
    index: int | None = None

    def openai_message(self) -> dict[str, Any]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments or "{}",
            },
        }


@dataclass
class WireResult:
    status: int | None = None
    elapsed_ms: int = 0
    finish_reasons: list[str] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    tool_deltas: int = 0
    missing_index_deltas: int = 0
    malformed_index_deltas: int = 0
    indexed_deltas: int = 0
    index_to_ids: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    id_to_indexes: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    content_chars: int = 0
    error: str | None = None

    @property
    def index_collisions(self) -> int:
        return sum(1 for ids in self.index_to_ids.values() if len(ids) > 1)

    @property
    def id_index_conflicts(self) -> int:
        return sum(1 for indexes in self.id_to_indexes.values() if len(indexes) > 1)


class _StreamAccumulator:
    """Accumulate OpenAI SSE tool-call deltas without retaining raw output."""

    def __init__(self) -> None:
        self.result = WireResult()
        self._calls: dict[tuple[str, Any], ToolCall] = {}
        self._index_keys: dict[tuple[int, int], tuple[str, Any]] = {}
        self._anonymous_key: tuple[str, int] | None = None

    def consume(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            finish = choice.get("finish_reason")
            if finish is not None:
                self.result.finish_reasons.append(str(finish))
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str):
                self.result.content_chars += len(content)
            raw_calls = delta.get("tool_calls")
            if isinstance(raw_calls, list):
                self._consume_calls(raw_calls, int(choice.get("index", 0) or 0))

        # This also supports a proxy that returns a complete JSON response
        # despite stream=true.
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                self.result.content_chars += len(content)
            raw_calls = message.get("tool_calls")
            if isinstance(raw_calls, list):
                self._consume_calls(raw_calls, int(choice.get("index", 0) or 0))

    def _consume_calls(self, raw_calls: list[Any], choice_index: int) -> None:
        for position, raw in enumerate(raw_calls):
            if not isinstance(raw, dict):
                continue
            self.result.tool_deltas += 1
            raw_index = raw.get("index")
            if raw_index is None:
                self.result.missing_index_deltas += 1
                index: int | None = None
            else:
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    self.result.malformed_index_deltas += 1
                    index = None
                else:
                    self.result.indexed_deltas += 1

            function = raw.get("function") or {}
            if not isinstance(function, dict):
                function = {}
            call_id = str(raw.get("id") or raw.get("call_id") or "").strip()
            if call_id:
                key: tuple[str, Any] = ("id", call_id)
            elif index is not None:
                key = self._index_keys.get(
                    (choice_index, index),
                    ("index", (choice_index, index)),
                )
            elif self._anonymous_key is not None:
                key = self._anonymous_key
            else:
                key = ("anonymous", position)
                self._anonymous_key = key

            call = self._calls.get(key)
            if call is None:
                call = ToolCall(
                    call_id=call_id or f"call-harness-{len(self._calls)}",
                    name="",
                    index=index,
                )
                self._calls[key] = call
            if index is not None:
                self._index_keys[(choice_index, index)] = key
            if index is not None:
                call.index = index
                if call.call_id:
                    self.result.index_to_ids[index].add(call.call_id)
                    self.result.id_to_indexes[call.call_id].add(index)
            name = function.get("name")
            if isinstance(name, str) and name:
                call.name += name
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                call.arguments += arguments

    def finish(self) -> WireResult:
        self.result.calls = list(self._calls.values())
        return self.result


async def _iter_sse_payloads(response: aiohttp.ClientResponse) -> AsyncIterator[str]:
    data_lines: list[str] = []

    async def flush() -> str | None:
        if not data_lines:
            return None
        value = "\n".join(data_lines)
        data_lines.clear()
        return value

    async for raw_line in response.content:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            payload = await flush()
            if payload is not None:
                yield payload
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    payload = await flush()
    if payload is not None:
        yield payload


async def _request_endpoint(
    http: aiohttp.ClientSession,
    *,
    url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    session_id: str | None,
    max_tokens: int,
) -> WireResult:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "omp-minimax-harness/1.0",
    }
    if session_id:
        headers["x-opencode-session"] = session_id
    body = {
        "model": model,
        "messages": messages,
        "tools": tool_definitions(),
        "tool_choice": "auto",
        "stream": True,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    started = time.monotonic()
    try:
        async with http.post(url, headers=headers, json=body) as response:
            result = WireResult(status=response.status)
            if response.status != 200:
                # Drain the body for connection reuse, but never expose it.
                await response.read()
                result.error = f"http_{response.status}"
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                return result

            accumulator = _StreamAccumulator()
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/event-stream" in content_type:
                async for payload in _iter_sse_payloads(response):
                    if payload == "[DONE]":
                        continue
                    try:
                        accumulator.consume(json.loads(payload))
                    except (TypeError, ValueError):
                        # An invalid event is a protocol failure, but keep the
                        # report bounded and free of the event body.
                        accumulator.result.error = "invalid_json_event"
            else:
                try:
                    accumulator.consume(await response.json())
                except (TypeError, ValueError, aiohttp.ContentTypeError):
                    accumulator.result.error = "invalid_json_response"
            result = accumulator.finish()
            result.status = response.status
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return WireResult(
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=type(exc).__name__,
        )


def _synthetic_tool_result(call: ToolCall) -> str:
    """Return a safe result that exercises the next model turn."""
    path = _skill_path(call.arguments)
    if path:
        return json.dumps({"status": "ok", "resource": path, "synthetic": True})
    return json.dumps({"status": "ok", "synthetic": True})


@dataclass
class ScenarioResult:
    endpoint: str
    compaction_mode: str
    turns_requested: int
    compaction_after: int
    session_id_sent: bool
    turn_results: list[WireResult] = field(default_factory=list)
    compactions: int = 0
    skill_reads: Counter[str] = field(default_factory=Counter)
    repeated_skill_reads: Counter[str] = field(default_factory=Counter)

    def summary(self) -> dict[str, Any]:
        status_counts = Counter(
            str(result.status) if result.status is not None else "no_response"
            for result in self.turn_results
        )
        errors = Counter(
            result.error for result in self.turn_results if result.error
        )
        return {
            "endpoint": self.endpoint,
            "compaction_mode": self.compaction_mode,
            "turns_requested": self.turns_requested,
            "turns_completed": len(self.turn_results),
            "compactions": self.compactions,
            "session_header_sent": self.session_id_sent,
            "status_counts": dict(sorted(status_counts.items())),
            "errors": dict(sorted(errors.items())),
            "tool_calls": sum(len(r.calls) for r in self.turn_results),
            "tool_deltas": sum(r.tool_deltas for r in self.turn_results),
            "skill_reads": dict(sorted(self.skill_reads.items())),
            "repeated_skill_reads": dict(sorted(self.repeated_skill_reads.items())),
            "wire_missing_index_deltas": sum(
                r.missing_index_deltas for r in self.turn_results
            ),
            "wire_malformed_index_deltas": sum(
                r.malformed_index_deltas for r in self.turn_results
            ),
            "wire_index_collisions": sum(
                r.index_collisions for r in self.turn_results
            ),
            "wire_id_index_conflicts": sum(
                r.id_index_conflicts for r in self.turn_results
            ),
            "finish_reasons": [
                reason
                for result in self.turn_results
                for reason in result.finish_reasons
            ],
            "latency_ms": [result.elapsed_ms for result in self.turn_results],
        }


async def run_scenario(
    http: aiohttp.ClientSession,
    *,
    endpoint: str,
    url: str,
    api_key: str,
    model: str,
    compaction_mode: str,
    turns: int,
    compaction_after: int,
    max_tokens: int,
    session_id: str | None,
) -> ScenarioResult:
    result = ScenarioResult(
        endpoint=endpoint,
        compaction_mode=compaction_mode,
        turns_requested=turns,
        compaction_after=compaction_after,
        session_id_sent=session_id is not None,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": INITIAL_PROMPT},
    ]
    loaded_skills: set[str] = set()

    for turn in range(1, turns + 1):
        wire = await _request_endpoint(
            http,
            url=url,
            api_key=api_key,
            model=model,
            messages=messages,
            session_id=session_id,
            max_tokens=max_tokens,
        )
        result.turn_results.append(wire)
        if wire.error:
            break

        for call in wire.calls:
            path = _skill_path(call.arguments)
            if call.name == "read" and path:
                result.skill_reads[path] += 1
                if path in loaded_skills:
                    result.repeated_skill_reads[path] += 1
                loaded_skills.add(path)

        if wire.calls:
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [call.openai_message() for call in wire.calls],
            }
            messages.append(assistant_message)
            for call in wire.calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": _synthetic_tool_result(call),
                })
        elif turn < turns:
            messages.append({
                "role": "assistant",
                "content": "[assistant response omitted by harness]",
            })
            messages.append({"role": "user", "content": CONTINUE_PROMPT})

        if turn == compaction_after and turn < turns:
            if compaction_mode == "preserved":
                state = ", ".join(sorted(loaded_skills)) or "none"
                summary = (
                    "OMP COMPACTION SUMMARY: completed skill resources are "
                    f"{state}. Do not read those resources again."
                )
            else:
                summary = (
                    "OMP COMPACTION SUMMARY: earlier tool outputs were archived; "
                    "the completed-resource list was not preserved. Continue the run."
                )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": summary},
                {"role": "user", "content": CONTINUE_PROMPT},
            ]
            result.compactions += 1

    return result


def replay_compaction_state(
    *,
    repeats: int,
    compaction_mode: str,
) -> dict[str, Any]:
    """Deterministically prove the compaction-state difference offline."""
    loaded = set(SKILL_PATHS)
    requested = list(SKILL_PATHS) * max(1, repeats)
    repeated = Counter(path for path in requested if path in loaded)
    if compaction_mode == "preserved":
        prevented = sum(repeated.values())
    else:
        # A lossy compaction forgets the completion set, so the next cycle is
        # eligible to repeat both reads.
        prevented = 0
    return {
        "mode": compaction_mode,
        "events": len(requested),
        "initially_loaded_skills": list(SKILL_PATHS),
        "eligible_repeats_after_compaction": sum(repeated.values()),
        "repeats_prevented_by_preserved_state": prevented,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call MiniMax and the gateway; never enabled implicitly.",
    )
    parser.add_argument(
        "--replay-only",
        action="store_true",
        help="Run only the deterministic compaction-state replay.",
    )
    parser.add_argument("--turns", type=int, default=5)
    parser.add_argument(
        "--compaction-after",
        type=int,
        default=2,
        help="Compact once after this completed turn.",
    )
    parser.add_argument(
        "--compaction-mode",
        choices=("both", "lossy", "preserved"),
        default="both",
    )
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument(
        "--direct-url",
        default="https://api.minimax.io/v1/chat/completions",
    )
    parser.add_argument(
        "--gateway-url",
        default="https://ai.tusker.net.au/v1/chat/completions",
    )
    parser.add_argument("--direct-model", default="MiniMax-M3")
    parser.add_argument("--gateway-model", default="minimax::MiniMax-M3")
    parser.add_argument(
        "--session-id",
        default="",
        help="Stable gateway session ID; generated if omitted.",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser


def _live_modes(value: str) -> tuple[str, ...]:
    return ("lossy", "preserved") if value == "both" else (value,)


async def _run_live(args: argparse.Namespace) -> dict[str, Any]:
    direct_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    gateway_key = (
        os.environ.get("GATEWAY_API_KEY", "").strip()
        or os.environ.get("TUSKER_GATEWAY_API_KEY", "").strip()
        or os.environ.get("API_KEYS", "").split(",", 1)[0].strip()
    )
    missing = []
    if not direct_key:
        missing.append("MINIMAX_API_KEY")
    if not gateway_key:
        missing.append("GATEWAY_API_KEY")
    if missing:
        raise RuntimeError("missing live credentials: " + ", ".join(missing))

    session_id = args.session_id.strip() or f"omp-harness-{uuid.uuid4().hex}"
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(ssl=True)
    results: list[dict[str, Any]] = []
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as http:
        for mode in _live_modes(args.compaction_mode):
            # Lossy and preserved compaction are independent conversations;
            # keep each gateway session stable within its own scenario while
            # preventing server-side state from crossing the comparison.
            mode_session_id = f"{session_id}-{mode}"
            direct = await run_scenario(
                http,
                endpoint="minimax-direct",
                url=args.direct_url,
                api_key=direct_key,
                model=args.direct_model,
                compaction_mode=mode,
                turns=args.turns,
                compaction_after=args.compaction_after,
                max_tokens=args.max_tokens,
                session_id=None,
            )
            gateway = await run_scenario(
                http,
                endpoint="gateway-minimax",
                url=args.gateway_url,
                api_key=gateway_key,
                model=args.gateway_model,
                compaction_mode=mode,
                turns=args.turns,
                compaction_after=args.compaction_after,
                max_tokens=args.max_tokens,
                session_id=mode_session_id,
            )
            results.extend((direct.summary(), gateway.summary()))
    return {
        "live": {
            "direct_url": args.direct_url,
            "gateway_url": args.gateway_url,
            "direct_model": args.direct_model,
            "gateway_model": args.gateway_model,
            "turns": args.turns,
            "compaction_after": args.compaction_after,
            "session_header_stable": True,
        },
        "results": results,
    }


def main() -> int:
    args = _build_parser().parse_args()
    if args.turns < 1 or args.compaction_after < 1 or args.compaction_after >= args.turns:
        print("turns must be >= 2 and compaction-after must be between 1 and turns-1", file=sys.stderr)
        return 2
    if args.live and args.replay_only:
        print("choose only one of --live and --replay-only", file=sys.stderr)
        return 2

    output: dict[str, Any] = {
        "replay": [
            replay_compaction_state(
                repeats=2,
                compaction_mode=mode,
            )
            for mode in _live_modes(args.compaction_mode)
        ]
    }
    if args.live:
        try:
            output.update(asyncio.run(_run_live(args)))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
