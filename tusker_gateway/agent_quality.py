"""Evidence-based scoring for agentic model turns.

The existing :mod:`tusker_gateway.quality` score measures whether an upstream
HTTP request succeeded and how quickly it started.  That is useful transport
health, but it cannot tell whether an OMP turn used its tools or completed the
requested task.  This module keeps those concerns separate and deliberately
represents missing task verification as ``hold`` rather than success.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


TaskStatus = Literal["passed", "failed", "unverified"]


def _bounded(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


@dataclass
class AgentEvidence:
    """Observable evidence collected from one OMP route or whole session."""

    route: str = "unknown"
    assistant_turns: int = 0
    provider_error_turns: int = 0
    tool_call_turns: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    successful_tool_results: int = 0
    failed_tool_results: int = 0
    unmatched_tool_results: int = 0
    final_text_turns: int = 0
    final_text_chars: int = 0
    leaked_tool_markup: int = 0
    protocol_failures: int = 0
    notes: list[str] = field(default_factory=list)

    def merge(self, other: "AgentEvidence") -> None:
        """Add another evidence set without changing this route label."""
        for name in (
            "assistant_turns",
            "provider_error_turns",
            "tool_call_turns",
            "tool_calls",
            "tool_results",
            "successful_tool_results",
            "failed_tool_results",
            "unmatched_tool_results",
            "final_text_turns",
            "final_text_chars",
            "leaked_tool_markup",
            "protocol_failures",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for note in other.notes:
            if note not in self.notes:
                self.notes.append(note)


@dataclass(frozen=True)
class AgentScore:
    """A score with explicit task-verification state."""

    route: str
    observed_score: float | None
    effective_score: float | None
    disposition: Literal["eligible", "hold", "reject"]
    task_status: TaskStatus
    dimensions: dict[str, float | None]
    evidence: dict[str, Any]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def score_agent_evidence(
    evidence: AgentEvidence,
    *,
    task_status: TaskStatus = "unverified",
    verbosity_budget_chars: int = 1200,
) -> AgentScore:
    """Score observable agent evidence.

    The score is intentionally not a language-quality judgement.  It measures:

    * protocol health (45%): provider/protocol failures per assistant turn;
    * tool execution (35%): successful tool results, counting missing results
      as failures; and
    * final-answer discipline (20%): bounded final prose length.

    Task completion is a separate gate.  ``unverified`` produces ``hold`` and
    no effective score; ``failed`` produces an effective score of zero.  This
    prevents a model from winning routing decisions merely because it emitted
    verbose prose after an unverified turn.
    """
    if task_status not in {"passed", "failed", "unverified"}:
        raise ValueError(f"invalid task status: {task_status!r}")

    turns = evidence.assistant_turns
    protocol_failures = evidence.provider_error_turns + evidence.protocol_failures
    protocol_score = (
        _bounded(100.0 * (1.0 - protocol_failures / turns))
        if turns
        else None
    )

    result_denominator = max(evidence.tool_calls, evidence.tool_results)
    if result_denominator:
        credited_results = max(
            0, evidence.successful_tool_results - evidence.unmatched_tool_results
        )
        tool_score = _bounded(
            100.0 * credited_results / result_denominator
        )
    elif evidence.tool_calls:
        tool_score = 0.0
    else:
        # No tool use is neutral for a non-agentic response.
        tool_score = None

    if evidence.final_text_turns:
        average_chars = evidence.final_text_chars / evidence.final_text_turns
        excess = max(0.0, average_chars - max(1, verbosity_budget_chars))
        # 2,400 chars over budget reaches zero; ordinary short answers score
        # 100.  Wordiness is a small signal, never a completion substitute.
        verbosity_score = _bounded(100.0 - (excess / 24.0))
    else:
        verbosity_score = None

    weighted = (
        (protocol_score, 0.45),
        (tool_score, 0.35),
        (verbosity_score, 0.20),
    )
    available_weight = sum(weight for value, weight in weighted if value is not None)
    observed_score = (
        sum(value * weight for value, weight in weighted if value is not None)
        / available_weight
        if available_weight
        else None
    )

    reasons: list[str] = []
    if evidence.provider_error_turns:
        reasons.append(
            f"{evidence.provider_error_turns} provider-error assistant turn(s)"
        )
    if evidence.protocol_failures:
        reasons.append(f"{evidence.protocol_failures} protocol failure(s)")
    if evidence.failed_tool_results:
        reasons.append(f"{evidence.failed_tool_results} failed tool result(s)")
    if evidence.tool_calls > evidence.tool_results:
        reasons.append(
            f"{evidence.tool_calls - evidence.tool_results} tool call(s) without a result"
        )
    if evidence.unmatched_tool_results:
        reasons.append(
            f"{evidence.unmatched_tool_results} tool result(s) without a matching call"
        )
    if evidence.leaked_tool_markup:
        reasons.append(f"{evidence.leaked_tool_markup} leaked tool-markup signal(s)")
    if verbosity_score is not None and verbosity_score < 70.0:
        reasons.append("final prose exceeded the configured verbosity budget")

    if task_status == "failed":
        effective_score = 0.0
        disposition: Literal["eligible", "hold", "reject"] = "reject"
        reasons.append("task verification failed")
    elif task_status == "passed":
        effective_score = observed_score
        disposition = "eligible" if effective_score is not None else "hold"
    else:
        effective_score = None
        disposition = "hold"
        reasons.append("task completion is unverified")

    if not reasons:
        reasons.append("no negative evidence observed")

    return AgentScore(
        route=evidence.route,
        observed_score=round(observed_score, 2) if observed_score is not None else None,
        effective_score=round(effective_score, 2) if effective_score is not None else None,
        disposition=disposition,
        task_status=task_status,
        dimensions={
            "protocol": round(protocol_score, 2) if protocol_score is not None else None,
            "tool_execution": round(tool_score, 2) if tool_score is not None else None,
            "verbosity": round(verbosity_score, 2) if verbosity_score is not None else None,
        },
        evidence=asdict(evidence),
        reasons=tuple(reasons),
    )


__all__ = ["AgentEvidence", "AgentScore", "TaskStatus", "score_agent_evidence"]
