"""Tests for evidence-based OMP agent scoring."""
from __future__ import annotations

import json

from tusker_gateway.agent_quality import AgentEvidence, score_agent_evidence
from tusker_gateway.omp_scoring import load_omp_evidence


def _message(role: str, **fields):
    return json.dumps({"type": "message", "message": {"role": role, **fields}})


def test_unverified_task_has_no_effective_success_score():
    evidence = AgentEvidence(
        route="Nvidia",
        assistant_turns=2,
        tool_calls=1,
        tool_results=1,
        successful_tool_results=1,
        final_text_turns=1,
        final_text_chars=100,
    )

    score = score_agent_evidence(evidence)

    assert score.observed_score is not None
    assert score.effective_score is None
    assert score.disposition == "hold"
    assert "task completion is unverified" in score.reasons


def test_empty_evidence_does_not_score_as_healthy():
    score = score_agent_evidence(AgentEvidence())

    assert score.observed_score is None
    assert score.effective_score is None
    assert score.disposition == "hold"


def test_failed_task_is_rejected_even_when_transport_looks_healthy():
    evidence = AgentEvidence(
        route="Nvidia",
        assistant_turns=1,
        final_text_turns=1,
        final_text_chars=100,
    )

    score = score_agent_evidence(evidence, task_status="failed")

    assert score.observed_score == 100.0
    assert score.effective_score == 0.0
    assert score.disposition == "reject"


def test_provider_and_tool_failures_reduce_observed_score():
    evidence = AgentEvidence(
        route="Cohere",
        assistant_turns=4,
        provider_error_turns=1,
        tool_calls=2,
        tool_results=2,
        successful_tool_results=1,
        failed_tool_results=1,
        final_text_turns=1,
        final_text_chars=4000,
    )

    score = score_agent_evidence(evidence, task_status="passed")

    assert score.dimensions["protocol"] == 75.0
    assert score.dimensions["tool_execution"] == 50.0
    assert score.dimensions["verbosity"] < 70.0
    assert score.effective_score == score.observed_score
    assert score.disposition == "eligible"


def test_omp_history_matches_tool_results_to_upstream_route():
    lines = [
        _message(
            "assistant",
            provider="tusker-gateway",
            model="hermes-code",
            upstreamProvider="Nvidia",
            stopReason="toolUse",
            content=[{"type": "toolCall", "id": "call-1", "name": "bash"}],
        ),
        _message(
            "toolResult",
            toolName="bash",
            toolCallId="call-1",
            isError=False,
            content=[{"type": "text", "text": "ok"}],
        ),
        _message(
            "assistant",
            provider="tusker-gateway",
            model="hermes-code",
            upstreamProvider="Nvidia",
            stopReason="stop",
            content=[{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "done"}],
        ),
    ]

    grouped = load_omp_evidence(lines)
    evidence = grouped["Nvidia"]

    assert evidence.assistant_turns == 2
    assert evidence.tool_calls == 1
    assert evidence.tool_results == 1
    assert evidence.successful_tool_results == 1
    assert evidence.failed_tool_results == 0
    assert evidence.final_text_chars == 4


def test_omp_history_preserves_explicit_tool_result_error():
    lines = [
        _message(
            "assistant",
            upstreamProvider="Cohere",
            stopReason="toolUse",
            content=[{"type": "toolCall", "id": "call-1", "name": "edit"}],
        ),
        _message(
            "toolResult",
            toolCallId="call-1",
            isError=True,
            content=[{"type": "text", "text": "failed"}],
        ),
    ]

    evidence = load_omp_evidence(lines)["Cohere"]

    assert evidence.failed_tool_results == 1
    assert evidence.successful_tool_results == 0


def test_unmatched_successful_tool_result_is_not_credited():
    lines = [
        _message(
            "toolResult",
            toolCallId="missing-call",
            isError=False,
            content=[{"type": "text", "text": "ok"}],
        ),
    ]

    evidence = load_omp_evidence(lines)["unknown"]
    score = score_agent_evidence(evidence, task_status="passed")

    assert evidence.unmatched_tool_results == 1
    assert score.dimensions["tool_execution"] == 0.0
    assert score.disposition == "eligible"
    assert any("without a matching call" in reason for reason in score.reasons)


def test_omp_history_can_scope_score_to_last_user_turn(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text(
        "\n".join(
            [
                _message("user", content=[{"type": "text", "text": "old"}]),
                _message(
                    "assistant",
                    upstreamProvider="Old",
                    stopReason="error",
                    content=[],
                ),
                _message("user", content=[{"type": "text", "text": "current"}]),
                _message(
                    "assistant",
                    upstreamProvider="Current",
                    stopReason="toolUse",
                    content=[{"type": "toolCall", "id": "call-1", "name": "bash"}],
                ),
                _message(
                    "toolResult",
                    toolCallId="call-1",
                    isError=False,
                    content=[{"type": "text", "text": "ok"}],
                ),
            ]
        )
        + "\n",
    )

    from tusker_gateway.omp_scoring import score_omp_session

    report = score_omp_session(session, last_user_turn=True)

    assert report["scope"] == "last_user_turn"
    assert "Old" not in report["routes"]
    assert report["routes"]["Current"]["evidence"]["tool_results"] == 1
