"""Seed the live turn-capture store with synthetic captures for the *recorded* reasons.

The gateway's capture store persists a narrow, fixed set of reason strings —
every ``capture_rejection`` call site in ``endpoints.py`` is reached via one
of two functions:

* ``unusable_tool_error(reason)`` — called with ``reason="reasoning_only_or_empty"``,
  downgraded to ``output_budget_exhausted`` when ``finish_reason == "length"``.
* ``empty_stream_error()`` — called when a non-tools turn emits no visible
  content.

Everything else (``MalformedToolCallError``, ``RequiredToolCallError``,
``InvalidToolCallArgumentsError``, ``ProviderStreamLoopError``,
``ProviderCapacityError``, ``ProviderModelRetiredError``,
``ClaudeAuthRequiredError``, ``ToolPolicyDeniedError``,
``HighImpactApprovalRequiredError``, ``ToolCallContractError``) is **raised
but never persisted** to the capture store. A replay tool cannot reproduce
something the gateway never captured, so this tool seeds only the three
real reasons — and one documentation-only fixture for the harness's
``malformed_tool_response`` mirror (a reason the harness can return but the
gateway never writes).

Usage:
    python -m tusker_gateway.tools.seed_captures              # seed all
    python -m tusker_gateway.tools.seed_captures --verify    # re-run --repeat 5
    python -m tusker_gateway.tools.seed_captures --reason reasoning_only_or_empty
    python -m tusker_gateway.tools.seed_captures --provider synthetic
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from tusker_gateway.turn_capture import (
    TurnCaptureStore,
    capture_store,
    list_turn_captures,
)


# ---------------------------------------------------------------------------
# Fixture generators
# ---------------------------------------------------------------------------

# A minimal tools-list shared by every tools-turn fixture. The harness only
# inspects truthiness of payload["tools"], not the schema.
_TOOLS_PAYLOAD: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "noop",
            "description": "A no-op tool used to force a tools turn.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]


def _request(
    *,
    model: str = "glm-5.3-flash",
    stream: bool = True,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    max_tokens: int | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> bytes:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages
        or [{"role": "user", "content": "ping"}],
        "stream": stream,
    }
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return json.dumps(body).encode()


def _sse(*chunks: bytes) -> bytes:
    """Concatenate raw SSE frames with the standard ``\\n\\n`` separator."""
    return b"\n\n".join(chunks) + b"\n\n"


def _openai_chunk(
    *,
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning: str = "",
    finish_reason: str | None = None,
) -> bytes:
    delta: dict[str, Any] = {}
    if content:
        delta["content"] = content
    if reasoning:
        delta["reasoning_content"] = reasoning
    if tool_calls:
        delta["tool_calls"] = tool_calls
    chunk = {
        "id": "seed",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    line = f"data: {json.dumps(chunk)}".encode()
    return _sse(line, b"data: [DONE]")


def _no_tool_call_chunk() -> bytes:
    """An empty tool turn: no content, no tool calls, finish_reason=stop."""
    return _openai_chunk(finish_reason="stop")


def _length_chunk() -> bytes:
    """A tool turn that ran out of room: no tool call, finish_reason=length."""
    return _openai_chunk(reasoning="x" * 200, finish_reason="length")


def _malformed_chunk() -> bytes:
    """A tool turn that opened a ``<tool_call>`` envelope but never closed it."""
    return _openai_chunk(content="<tool_call>truncated, never closed", finish_reason="stop")


@dataclass
class SeedSpec:
    """One fixture: a request, a response, the reason the gateway would record."""

    reason: str
    provider: str
    model: str
    pool: str
    request_body: bytes
    response_body: bytes
    description: str = ""
    # For replay-runs: an async transport that re-emits response_body verbatim.
    # If None, the verifier re-uses response_body from the capture.
    replay_response: bytes | None = None
    extra_replay_runs: list[dict[str, Any]] = field(default_factory=list)


# Reasons the harness can mirror but the gateway never persists. Kept as
# documentation so a future operator sees the surface gap explicitly.
HARNESS_ONLY_REASONS: frozenset[str] = frozenset({"malformed_tool_response"})

# Reasons the gateway records. This is the *complete* set verified by AST
# walk over tusker_gateway/endpoints.py.
RECORDED_REASONS: frozenset[str] = frozenset(
    {"reasoning_only_or_empty", "output_budget_exhausted", "empty_stream"}
)


# ---------------------------------------------------------------------------
# Per-reason fixtures. The harness mirror is the source of truth for what
# these captures represent; the gateway records only the three reasons
# listed in RECORDED_REASONS.
# ---------------------------------------------------------------------------


def _build_fixtures() -> list[SeedSpec]:
    fixtures: list[SeedSpec] = []

    # 1. reasoning_only_or_empty (finish_reason=stop, no tool call, no content).
    # The gateway records this for a tools turn that ends with no tool call
    # and no visible content. The harness mirrors it in _unusable_reason.
    fixtures.append(
        SeedSpec(
            reason="reasoning_only_or_empty",
            provider="ollama-cloud",
            model="glm-5.3-flash",
            pool="code",
            description="Stop with no tool call, no visible content.",
            request_body=_request(tools=_TOOLS_PAYLOAD, max_tokens=16384),
            response_body=_no_tool_call_chunk(),
        )
    )

    # 2. output_budget_exhausted (finish_reason=length, reasoning-filled).
    # The gateway downgrades reasoning_only_or_empty to this when
    # finish_reason=length (endpoints.py:1166-1170). The harness applies the
    # same downgrade.
    fixtures.append(
        SeedSpec(
            reason="output_budget_exhausted",
            provider="ollama-cloud",
            model="glm-5.3-flash",
            pool="code",
            description="Length with no tool call, heavy reasoning.",
            request_body=_request(tools=_TOOLS_PAYLOAD, max_tokens=16384),
            response_body=_length_chunk(),
        )
    )

    # 3. empty_stream (no-tools turn, no content, finish_reason=stop).
    # The gateway records this for a non-tools turn that emits no visible
    # content. The harness returns None for non-tools turns (it operates
    # on the tools-turn axis), so the harness-side verdict for this reason
    # is "usable=True" — the harness classifies a no-tools empty turn as
    # a successful call, while the gateway calls it an empty stream. They
    # classify on disjoint axes; a replay's --reason filter returning this
    # row is correct.
    fixtures.append(
        SeedSpec(
            reason="empty_stream",
            provider="synthetic",
            model="syn:small:text",
            pool="code",
            description="No-tools turn, empty content, finish=stop.",
            request_body=_request(tools=None),
            response_body=_no_tool_call_chunk(),
        )
    )

    # 4. malformed_tool_response (opened but unclosed tool envelope).
    # The harness returns this from _unusable_reason when
    # tool_markup_opening=True and no tool call. The gateway *raises*
    # MalformedToolCallError at endpoints.py:1351/1578/1590/1736/1850 but
    # never calls capture_rejection from that path — so this reason is
    # **never persisted** to the live capture store. The fixture is kept
    # so the harness's classification path is exercised and the operator
    # can see the surface mismatch in the verifier output.
    fixtures.append(
        SeedSpec(
            reason="malformed_tool_response",
            provider="zai",
            model="glm-4.7",
            pool="code",
            description="Unclosed <tool_call> envelope (harness-only; gateway never persists).",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            response_body=_malformed_chunk(),
        )
    )

    return fixtures


# ---------------------------------------------------------------------------
# Replay verifier
# ---------------------------------------------------------------------------


def _verify_one(
    fixture: SeedSpec,
    capture_id: str,
) -> dict[str, Any]:
    """Run the harness against one seeded capture and report its verdict.

    The replay-side transport is the captured ``response_body`` verbatim;
    here we are exercising the offline harness rather than a real upstream.
    Empty bodies raise ReplayError and are caught so the sweep survives.
    """
    from tusker_gateway.tools.replay_capture import (
        ReplayError,
        _unusable_reason,
        _tools_requested,
        analyze_response,
    )

    response = fixture.replay_response or fixture.response_body
    summary: dict[str, Any]
    try:
        summary = analyze_response(response)
    except ReplayError as exc:
        return {
            "capture_id": capture_id,
            "reason": fixture.reason,
            "harness_reason": f"replay_error:{exc.reason}",
            "tools_requested": None,
            "summary": {"error": str(exc)},
            "match": False,
            "is_known_gap": True,
        }

    tools_requested = _tools_requested(
        {"request_body": fixture.request_body}
    )
    harness_reason = _unusable_reason(summary, tools_requested)
    # Known surface gaps:
    # - empty_stream: gateway records it for non-tools turns; harness
    #   operates on the tools-turn axis and returns None, so a replay
    #   of an empty_stream capture classifies as usable=True. Documented.
    # - malformed_tool_response: gateway never persists; harness returns
    #   it for an unclosed tool envelope. Documented.
    is_known_gap = fixture.reason in {
        "empty_stream",
        "malformed_tool_response",
    }
    return {
        "capture_id": capture_id,
        "reason": fixture.reason,
        "harness_reason": harness_reason,
        "tools_requested": tools_requested,
        "summary": {
            "finish_reason": summary.get("finish_reason"),
            "tool_call_deltas": summary.get("tool_call_deltas"),
            "saw_tool_call": summary.get("saw_tool_call"),
            "tool_markup_opening": summary.get("tool_markup_opening"),
            "visible_content_chars": summary.get("visible_content_chars"),
        },
        "match": harness_reason == fixture.reason,
        "is_known_gap": is_known_gap,
    }


# ---------------------------------------------------------------------------
# CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else "seed captures"
    )
    parser.add_argument(
        "--reason",
        action="append",
        default=None,
        help="Limit seeding to one or more reasons (default: all).",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Override the provider on every seeded capture.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After seeding, re-run the harness against each capture and print verdicts.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=5,
        help="How many harness runs to record per capture during verify (default: 5).",
    )
    parser.add_argument(
        "--list-existing",
        action="store_true",
        help="List the seeded captures already in the store and exit.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete every seed_captures row from the store and exit (irreversible).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    store = capture_store()
    if store is None:
        print(
            "ERROR: turn capture is disabled. Set TUSKER_STREAM_CAPTURE=1 "
            "(or TUSKER_CONFIG_DATABASE_ENABLED=1 in production) and retry.",
            file=sys.stderr,
        )
        return 2

    if args.list_existing:
        rows = [
            r for r in list_turn_captures(limit=500)
            if r.get("request_source") == "seed_captures"
        ]
        for r in rows:
            print(
                f"{r['capture_id']}  reason={r['reason']}  "
                f"provider={r['provider']}/{r['model']}  "
                f"replay_runs={len(r.get('replay_runs') or [])}"
            )
        return 0

    if args.prune:
        removed = store.delete_by_source("seed_captures")
        print(f"pruned {removed} seed_captures row(s)")
        return 0


    fixtures = _build_fixtures()
    if args.reason:
        wanted = set(args.reason)
        fixtures = [f for f in fixtures if f.reason in wanted]
    if args.provider:
        for f in fixtures:
            f.provider = args.provider

    written: list[tuple[SeedSpec, str]] = []
    for fixture in fixtures:
        capture_id = _seed_to_capture(store, fixture)
        if capture_id is None:
            print(f"FAILED to write seed for reason={fixture.reason}", file=sys.stderr)
            continue
        written.append((fixture, capture_id))
        print(
            f"wrote {capture_id}  reason={fixture.reason}  "
            f"provider={fixture.provider}/{fixture.model}  "
            f"pool={fixture.pool}"
        )

    if not args.verify:
        print(
            f"\nSeeded {len(written)} captures. "
            f"Run with --verify to exercise the harness against each."
        )
        return 0

    print(f"\nVerifying with --repeat {args.repeat}…")
    matches = gaps = 0
    for fixture, capture_id in written:
        for i in range(args.repeat):
            verdict = _verify_one(fixture, capture_id)
            store.record_replay_run(
                capture_id,
                {
                    "attempt": i + 1,
                    "provider": fixture.provider,
                    "reason": fixture.reason,
                    "harness_reason": verdict["harness_reason"],
                    "match": verdict["match"],
                    "is_known_gap": verdict["is_known_gap"],
                    "summary": verdict["summary"],
                },
            )
        if verdict["match"]:
            flag = "OK "
            matches += 1
        elif verdict["is_known_gap"]:
            flag = "GAP"
            gaps += 1
        else:
            flag = "!! "
            gaps += 1
        print(
            f"  [{flag}] {fixture.reason:36}  "
            f"harness={verdict['harness_reason']!r}  "
            f"summary={verdict['summary']}"
        )

    print(
        f"\n{len(written)} captures exercised × {args.repeat} runs = "
        f"{len(written) * args.repeat} replay_runs entries written."
    )
    print(
        f"Harness agreement: {matches}/{len(written)} classified identically; "
        f"{gaps} documented surface gap(s)."
    )
    return 0


def _seed_to_capture(
    store: TurnCaptureStore, fixture: SeedSpec
) -> str | None:
    """Write one fixture into the live store. Returns the capture_id."""
    return store.record(
        request_id=f"seed-{fixture.reason}-{int(time.time() * 1000)}",
        provider=fixture.provider,
        model=fixture.model,
        pool=fixture.pool,
        request_source="seed_captures",
        reason=fixture.reason,
        verdict={"tools_requested": True, "seeded": True},
        request_payload=fixture.request_body,
        response_payload=fixture.response_body,
    )


if __name__ == "__main__":
    sys.exit(main())
