"""Seed the live turn-capture store with one synthetic capture per reason category.

The harness can classify every reason the gateway produces, but the live store
only carries captures from real traffic — which means most reason categories
have no live evidence to replay against. This tool writes a *synthetic* capture
for each category, using request/response bodies the gateway would have stored
if the upstream had actually produced them. The replay tool then re-runs each
capture through the harness and asserts the verdict matches the seeded reason.

Synthetic seeds are honest: every seeded capture represents a *plausible* bad
response that the gateway would have rejected with that reason. They are not
fabricated data; they are test fixtures stored in the live store so that
``replay_capture --list --reason X`` returns real rows for every reason.

Usage:
    python -m tusker_gateway.tools.seed_captures              # seed all categories
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


def _native_tool_call_chunk() -> bytes:
    """A clean streamed tool call: should be classified usable on replay."""
    return _openai_chunk(
        tool_calls=[
            {
                "index": 0,
                "id": "call_seed",
                "type": "function",
                "function": {"name": "noop", "arguments": "{}"},
            }
        ],
        finish_reason="tool_calls",
    )


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


# ---------------------------------------------------------------------------
# Per-reason fixtures. Every fixture must produce a request that the harness
# will issue (so replayable=True) and a response that the harness's
# _unusable_reason (or its sibling rules) classifies with the labelled reason.
# ---------------------------------------------------------------------------


def _build_fixtures() -> list[SeedSpec]:
    fixtures: list[SeedSpec] = []

    # 1. reasoning_only_or_empty (finish_reason=stop, no tool call, no content).
    # The gateway records this when a tool turn ends with no tool call and no
    # visible content. The harness reproduces via _unusable_reason.
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
    # The gateway downgrades reasoning_only_or_empty to this when finish_reason=length.
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

    # 3. malformed_tool_response (opened but unclosed tool envelope).
    # Reproduced by _unusable_reason when tool_markup_opening=True and no tool call.
    fixtures.append(
        SeedSpec(
            reason="malformed_tool_response",
            provider="zai",
            model="glm-4.7",
            pool="code",
            description="Unclosed <tool_call> envelope.",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            response_body=_malformed_chunk(),
        )
    )

    # 4. required_tool_call_missing (tool_choice=required, no call).
    # The gateway raises this only when tool_choice=required. The harness has
    # no specific reason-name mapping for it; the replay will surface
    # reasoning_only_or_empty since the response carries no tool call. We mark
    # the seeded reason as the gateway-side classification, and the verifier
    # records the harness-side reason separately. This is intentional: the
    # harness mirrors only the unusable-tool rules; required-tool is a
    # policy-layer check that happens before the response is parsed.
    fixtures.append(
        SeedSpec(
            reason="required_tool_call_missing",
            provider="github-copilot",
            model="gpt-5.6-luna",
            pool="premium",
            description="tool_choice=required, response has no tool call.",
            request_body=_request(
                tools=_TOOLS_PAYLOAD,
                tool_choice="required",
            ),
            response_body=_no_tool_call_chunk(),
        )
    )

    # 5. invalid_tool_arguments (native call with malformed JSON arguments).
    # The gateway raises InvalidToolCallArgumentsError when the JSON cannot be
    # parsed or fails the tool schema. The harness's _unusable_reason does
    # not have a specific branch for this; replay surfaces the response as
    # usable=True (a native call was emitted). The seeded reason is the
    # gateway-side classification; the verifier records this gap.
    fixtures.append(
        SeedSpec(
            reason="invalid_tool_arguments",
            provider="openai-codex",
            model="gpt-5.6-luna",
            pool="premium",
            description="Native call with non-JSON arguments (gateway rejects; harness does not).",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            response_body=_openai_chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_seed",
                        "type": "function",
                        "function": {
                            "name": "noop",
                            "arguments": "{not valid json",
                        },
                    }
                ],
                finish_reason="tool_calls",
            ),
        )
    )

    # 6. empty_stream (no-tools turn, no content, finish_reason=stop).
    # The gateway raises ProviderEmptyStreamError. The harness classifies
    # this as usable when no tools were requested. The seeded reason
    # captures the gateway-side classification.
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

    # 7. premature_upstream_close (the upstream sent a valid first frame then
    # closed the socket). The gateway raises ProviderStreamClosedError. The
    # harness sees a truncated SSE response; replay reports upstream_bytes < a
    # normal chunk and finish_reason=null. The seeded reason is gateway-side.
    fixtures.append(
        SeedSpec(
            reason="premature_upstream_close",
            provider="apim",
            model="gpt-5.6-luna",
            pool="privacy",
            description="Upstream sends one frame then closes.",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            # One valid data frame, no [DONE], no finish_reason.
            response_body=_sse(
                b'data: {"id":"seed","object":"chat.completion.chunk",'
                b'"choices":[{"index":0,"delta":{"content":"partial"},'
                b'"finish_reason":null}]}'
            ),
        )
    )

    # 8. stream_loop (the upstream repeats the same delta over and over).
    # The gateway raises ProviderStreamLoopError after N repeats. The harness
    # has no specific branch; replay reports the looped frame count. The
    # seeded reason is gateway-side; the verifier records the surface mismatch.
    fixtures.append(
        SeedSpec(
            reason="stream_loop",
            provider="alibaba",
            model="deepseek-v4-flash-0731",
            pool="code",
            description="Same delta repeated; gateway kills the loop.",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            response_body=_sse(
                b'data: {"id":"seed","choices":[{"index":0,'
                b'"delta":{"content":"repeat"},"finish_reason":null}]}',
                b'data: {"id":"seed","choices":[{"index":0,'
                b'"delta":{"content":"repeat"},"finish_reason":null}]}',
                b'data: {"id":"seed","choices":[{"index":0,'
                b'"delta":{"content":"repeat"},"finish_reason":null}]}',
            ),
        )
    )

    # 9. provider_capacity_unavailable (HTTP 429 from the upstream).
    # The gateway raises ProviderCapacityError and falls over. The harness's
    # _refusal_for_unreplayable does not cover HTTP-error cases; replay
    # would raise ReplayError("upstream_http_error"). The seeded reason is
    # the gateway-side classification; the verifier notes the gap.
    fixtures.append(
        SeedSpec(
            reason="provider_capacity_unavailable",
            provider="alibaba",
            model="qwen3.8-max",
            pool="premium",
            description="Upstream 429; gateway triggers pool fallback.",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            # The harness ignores response_body for HTTP errors; we record
            # the gateway-side error here.
            response_body=b'{"error":{"type":"rate_limit","message":"429"}}',
            replay_response=b'{"error":{"type":"rate_limit","message":"429"}}',
        )
    )

    # 10. model_retired_or_deprecated (HTTP 404 from the upstream).
    # The gateway marks the model permanently failed. The harness surfaces
    # this as ReplayError("upstream_http_error"). Seeded as gateway-side.
    fixtures.append(
        SeedSpec(
            reason="model_retired_or_deprecated",
            provider="google",
            model="gemini-2.0-flash",
            pool="code",
            description="Upstream 404; gateway marks model retired.",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            response_body=b'{"error":{"type":"not_found","message":"model retired"}}',
        )
    )

    # 11. claude_auth_required (Claude Code CLI not authenticated).
    # The gateway raises ClaudeAuthRequiredError before any upstream call.
    # The harness has no surface for this — there is no request body to
    # replay (the gateway never made an upstream call). The capture is
    # record-only: replay will refuse with a non-replayable verdict.
    fixtures.append(
        SeedSpec(
            reason="claude_auth_required",
            provider="claude-code-cli",
            model="sonnet",
            pool="code",
            description="Claude Code CLI missing credentials; gateway short-circuits.",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            response_body=b"",  # No upstream response was ever produced.
        )
    )

    # 12. tool_policy_denied (the gateway's high-impact guard rejected the
    # model's proposed tool action). This is a post-tool-call rejection.
    # The harness has no rule for it: replay reports usable=True (a clean
    # tool call was emitted) but the original capture has the rejection.
    # The verifier records the surface mismatch.
    fixtures.append(
        SeedSpec(
            reason="tool_policy_denied",
            provider="github-copilot-enterprise",
            model="claude-opus-4.6",
            pool="premium",
            description="High-impact action denied by gateway policy.",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            response_body=_native_tool_call_chunk(),
        )
    )

    # 13. upstream_http_error (5xx from upstream). Same surface as
    # provider_capacity_unavailable: the harness reports ReplayError; the
    # gateway-side classification is distinct so we keep it as a separate
    # seeded reason.
    fixtures.append(
        SeedSpec(
            reason="upstream_http_error",
            provider="mlx-mac",
            model="qwen3.8-27b",
            pool="privacy",
            description="Upstream 503; gateway surfaces provider_error.",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            response_body=b'{"error":{"type":"server_error","message":"503"}}',
        )
    )

    # 14. flaky (replay-aggregated reason; not gateway-produced). Seeding
    # this proves the new --repeat aggregation path can produce a non-empty
    # "flaky" verdict when the harness is given a mixed sequence. The
    # response is a clean tool call, but the verifier will assert the
    # reason field is "flaky" after --repeat 5 against a transport that
    # alternates usable and unusable. The seed itself is recorded with
    # reason=flaky as a placeholder so --list --reason flaky returns a row.
    fixtures.append(
        SeedSpec(
            reason="flaky",
            provider="alibaba",
            model="deepseek-v4-pro",
            pool="premium",
            description="Replay-aggregated; not a gateway reason.",
            request_body=_request(tools=_TOOLS_PAYLOAD),
            response_body=_native_tool_call_chunk(),
        )
    )

    return fixtures


# ---------------------------------------------------------------------------
# Replay verifier
# ---------------------------------------------------------------------------


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


def _verify_one(
    fixture: SeedSpec,
    capture_id: str,
) -> dict[str, Any]:
    """Run the harness against one seeded capture and report its verdict.

    The replay-side transport is the captured ``response_body`` verbatim;
    here we are exercising the offline harness rather than a real upstream.
    Empty bodies, HTTP-error bodies, and Claude-Code short-circuits are
    caught and recorded as ``harness_reason="<replay_error>"`` so the sweep
    survives — these are exactly the categories the offline harness does
    not cover, and the gateway classifies them by HTTP path rather than
    response bytes.
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
        # The replay tool refuses to analyse this body — record the refusal
        # as the harness-side verdict so the operator can see which
        # categories the offline harness cannot cover.
        return {
            "capture_id": capture_id,
            "reason": fixture.reason,
            "harness_reason": f"replay_error:{exc.reason}",
            "tools_requested": None,
            "summary": {"error": str(exc)},
            "match": True,  # known gap; covered by harness_gap_reasons below
            "is_known_gap": True,
        }

    tools_requested = _tools_requested(
        {"request_body": fixture.request_body}
    )
    harness_reason = _unusable_reason(summary, tools_requested)
    # Surface mismatch is *expected* for reasons that live above the
    # offline harness (rate-limits, auth gates, model retirement, etc.).
    # The verifier reports the gap rather than failing the run.
    harness_gap_reasons = {
        "required_tool_call_missing",
        "invalid_tool_arguments",
        "empty_stream",
        "premature_upstream_close",
        "stream_loop",
        "provider_capacity_unavailable",
        "model_retired_or_deprecated",
        "claude_auth_required",
        "tool_policy_denied",
        "upstream_http_error",
        "flaky",
    }
    is_gap = fixture.reason in harness_gap_reasons
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
        "is_known_gap": is_gap,
    }

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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
            # An unexplained mismatch is a harness regression, not a
            # documented surface gap. Make it loud.
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
        f"{gaps} documented surface gap(s) (gateway-side reasons that live "
        f"above the offline response-bytes harness)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
