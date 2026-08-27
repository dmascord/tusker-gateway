"""Score an OMP JSONL session using only recorded evidence.

Usage::

    python -m tusker_gateway.omp_scoring /path/to/session.jsonl
    python -m tusker_gateway.omp_scoring /path/to/session.jsonl --task-status failed

The default ``unverified`` status is deliberate.  OMP history can prove that
tools were called and whether tool results reported errors, but it cannot prove
that a requested repository or deployment state was achieved without an
external verifier.
"""
from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from tusker_gateway.agent_quality import (
    AgentEvidence,
    TaskStatus,
    score_agent_evidence,
)


def _route_for_message(message: dict[str, Any]) -> str:
    """Prefer a concrete upstream label, falling back to OMP's logical model."""
    for key in ("upstreamModel", "backendModel", "upstreamProvider", "model", "provider"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _tool_call_id(block: dict[str, Any]) -> str | None:
    for key in ("id", "toolCallId", "tool_call_id"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _has_tool_markup(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "<tool_call",
            "<function=",
            "<parameter=",
            "tool_call:",
        )
    )


def load_omp_evidence(lines: Iterable[str]) -> dict[str, AgentEvidence]:
    """Load OMP messages and return evidence grouped by upstream route.

    Tool results are matched by ``toolCallId`` where available.  OMP versions
    that omit the id are handled with a FIFO fallback so the report remains
    useful without inventing a successful result.
    """
    routes: dict[str, AgentEvidence] = {}
    pending_by_id: dict[str, str] = {}
    pending_fifo: deque[tuple[str, str]] = deque()
    last_route = "unknown"

    def evidence_for(route: str) -> AgentEvidence:
        if route not in routes:
            routes[route] = AgentEvidence(route=route)
        return routes[route]

    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "message":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")

        if role == "assistant":
            route = _route_for_message(message)
            last_route = route
            evidence = evidence_for(route)
            evidence.assistant_turns += 1
            if message.get("stopReason") == "error":
                evidence.provider_error_turns += 1

            tool_blocks = [block for block in _blocks(message) if block.get("type") == "toolCall"]
            if tool_blocks:
                evidence.tool_call_turns += 1
                evidence.tool_calls += len(tool_blocks)
                for index, block in enumerate(tool_blocks):
                    call_id = _tool_call_id(block) or f"{event.get('timestamp', '')}:{index}"
                    pending_by_id[call_id] = route
                    pending_fifo.append((call_id, route))

            text_blocks = [
                block.get("text")
                for block in _blocks(message)
                if block.get("type") == "text" and isinstance(block.get("text"), str)
            ]
            if message.get("stopReason") == "stop" and text_blocks:
                text = "".join(text_blocks)
                evidence.final_text_turns += 1
                evidence.final_text_chars += len(text)
                if _has_tool_markup(text):
                    evidence.leaked_tool_markup += 1

        elif role == "toolResult":
            call_id = message.get("toolCallId")
            route = pending_by_id.pop(call_id, None) if isinstance(call_id, str) else None
            unmatched = False
            if route is None:
                route = last_route
                if pending_fifo:
                    _, fifo_route = pending_fifo.popleft()
                    route = fifo_route
                else:
                    unmatched = True
            else:
                pending_fifo = deque((pending_id, pending_route) for pending_id, pending_route in pending_fifo if pending_id != call_id)

            evidence = evidence_for(route)
            evidence.tool_results += 1
            is_error = message.get("isError") is True
            if is_error:
                evidence.failed_tool_results += 1
            else:
                evidence.successful_tool_results += 1
            if unmatched:
                evidence.unmatched_tool_results += 1

    return routes


def score_omp_session(
    path: str | Path,
    *,
    task_status: TaskStatus = "unverified",
    verbosity_budget_chars: int = 1200,
    last_user_turn: bool = False,
) -> dict[str, Any]:
    """Return a JSON-serializable score report for an OMP JSONL session."""
    session_path = Path(path)
    with session_path.open("r", encoding="utf-8") as handle:
        lines = list(handle)

    scope = "session"
    if last_user_turn:
        parsed: list[dict[str, Any]] = []
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                parsed.append(event)
        user_indexes = [
            index
            for index, event in enumerate(parsed)
            if event.get("type") == "message"
            and isinstance(event.get("message"), dict)
            and event["message"].get("role") == "user"
        ]
        if user_indexes:
            lines = [json.dumps(event) for event in parsed[user_indexes[-1]:]]
        else:
            lines = []
        scope = "last_user_turn"

    grouped = load_omp_evidence(lines)

    aggregate = AgentEvidence(route="all")
    scores: dict[str, Any] = {}
    for route in sorted(grouped):
        evidence = grouped[route]
        aggregate.merge(evidence)
        scores[route] = score_agent_evidence(
            evidence,
            task_status=task_status,
            verbosity_budget_chars=verbosity_budget_chars,
        ).to_dict()

    return {
        "session": str(session_path),
        "scope": scope,
        "task_status": task_status,
        "overall": score_agent_evidence(
            aggregate,
            task_status=task_status,
            verbosity_budget_chars=verbosity_budget_chars,
        ).to_dict(),
        "routes": scores,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="OMP session JSONL path")
    parser.add_argument(
        "--task-status",
        choices=("passed", "failed", "unverified"),
        default="unverified",
        help="external task-verification result (default: unverified)",
    )
    parser.add_argument(
        "--verbosity-budget-chars",
        type=int,
        default=1200,
        help="average final-answer character budget before a penalty",
    )
    parser.add_argument(
        "--last-user-turn",
        action="store_true",
        help="score only events after the most recent user message",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    report = score_omp_session(
        args.session,
        task_status=args.task_status,
        verbosity_budget_chars=args.verbosity_budget_chars,
        last_user_turn=args.last_user_turn,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        overall = report["overall"]
        print(
            f"overall observed={overall['observed_score']} "
            f"effective={overall['effective_score']} "
            f"disposition={overall['disposition']}"
        )
        for route, score in report["routes"].items():
            print(
                f"{route}: observed={score['observed_score']} "
                f"effective={score['effective_score']} "
                f"disposition={score['disposition']}"
            )
            for reason in score["reasons"]:
                print(f"  - {reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["load_omp_evidence", "main", "score_omp_session"]
