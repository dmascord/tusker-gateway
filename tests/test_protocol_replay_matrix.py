"""CI gate for the deterministic protocol replay matrix.

``tusker_gateway.tools.protocol_replay_matrix`` drives the gateway's real
HTTP/SSE surface against a scripted provider simulator (a live loopback
aiohttp upstream) and compares each response against a golden contract.
This test makes the matrix a hard gate rather than an operator-only tool:
every scenario must pass, and the scenario set must cover the protocol
surface the runbook enumerates.

Hermetic: the simulator binds loopback, credentials are fixed dummy values,
and the quality/cooldown state is reset per scenario inside the matrix.
"""
from __future__ import annotations

from tusker_gateway.tools.protocol_replay_matrix import run_matrix, scenarios

# The scenario set the runbook (docs/codex-protocol-completion-runbook.md,
# Phase 2.3) requires to be covered. Pinned so a refactor that silently
# drops a case fails here instead of quietly shrinking the gate.
REQUIRED_SCENARIOS = {
    "plain_text_stream",
    "plain_text_nonstream",
    "reasoning_only_stream",
    "reasoning_then_content_stream",
    "reasoning_alias_reasoning_field",
    "native_tool_call_stream",
    "native_tool_call_nonstream",
    "split_json_arguments_stream",
    "parallel_tool_calls_stream",
    "missing_finish_reason_stream",
    "duplicate_dsml_markup_in_content_stream",
    "empty_content_stream",
    "malformed_sse_frame",
    "tool_result_continuation_nonstream",
    "upstream_500",
    "upstream_429",
    "mid_stream_close",
}
# Driven by separate runners (the access log and the Responses route
# respectively), so they are asserted against the matrix results rather
# than ``scenarios()``.
REQUIRED_EXTRA_SCENARIOS = {
    "operator_diagnostics",
    "responses_pairing",
    # Runbook 2.3 lists cancellation and retry alongside the response-shape
    # fixtures. Both are lifecycle contracts driven directly by the matrix.
    "client_cancellation",
    "transient_retry_recovery",
}

# Failure-shaped fixtures: their contract is the failure surface itself, so
# they are deliberately outside the cross-provider failover sweep.
FAILURE_SCENARIOS = {"upstream_500", "upstream_429", "mid_stream_close"}


def test_replay_matrix_covers_required_scenarios():
    names = {scenario.name for scenario in scenarios()}
    assert REQUIRED_SCENARIOS <= names, REQUIRED_SCENARIOS - names


async def test_replay_matrix_passes_all_contracts():
    matrix = await run_matrix(failover=True)
    failures = [
        f"{row['scenario']}: {'; '.join(row['failures'])}"
        for row in matrix["results"]
        if not row["passed"]
    ]
    assert failures == []
    assert matrix["passed"] == matrix["scenarios"] > 0
    matrix_names = {r["scenario"] for r in matrix["results"]}
    assert REQUIRED_EXTRA_SCENARIOS <= matrix_names, (
        REQUIRED_EXTRA_SCENARIOS - matrix_names
    )
    # Runbook Phase 2.4: every success-path fixture must run single-upstream
    # *and* under cross-provider failover. Pinned so a refactor cannot drop
    # the failover sweep back to a single token scenario. Failure-shaped
    # fixtures are excluded by design (a clean fallback would contradict
    # their contract; tests/test_fault_scenarios.py covers their failover).
    failover_covered = {
        name.removeprefix("failover/")
        for name in matrix_names
        if name.startswith("failover/")
    }
    assert failover_covered == REQUIRED_SCENARIOS - FAILURE_SCENARIOS, (
        REQUIRED_SCENARIOS - FAILURE_SCENARIOS - failover_covered
    )
    # Runbook Phase 2.4: every captured golden request must replay in both
    # modes and under failover. Pinned per captured request so a fixture that
    # stops being replayed (or a runner that silently skips it) fails here.
    from tusker_gateway.tools.golden_replay import load_golden_requests

    for label, _captured in load_golden_requests():
        for mode in ("stream", "nonstream", "failover"):
            expected = f"golden/{label}/{mode}"
            assert expected in matrix_names, (
                f"golden replayed row missing: {expected}"
            )


def test_access_log_record_check_passes_on_a_clean_record():
    """A well-formed diagnostics record produces no violations."""
    from tusker_gateway.tools.protocol_replay_matrix import (
        _access_log_record_failures,
    )

    clean = {
        "provider": "alpha", "model": "sim-model", "route_kind": "pool",
        "protocol_family": "openai", "protocol_confidence": "provider",
        "endpoint_id": "abc123", "evidence_version": "protocol-contract-v1",
        "selected_adapter": "openai", "candidate_attempts": 1, "status": 200,
    }
    assert _access_log_record_failures(clean) == []


def test_access_log_record_check_catches_content_and_secrets():
    """The leak assertion must fail on the shapes it exists to catch.

    Exercised directly: driving a real leak through the HTTP stack would
    mean mutating the gateway's own logger, and an assertion that can never
    be made to fail is not a check.
    """
    from tusker_gateway.tools.protocol_replay_matrix import (
        _access_log_record_failures,
    )

    base = {
        "provider": "alpha", "model": "sim-model", "route_kind": "pool",
        "protocol_family": "openai", "protocol_confidence": "provider",
        "endpoint_id": "abc123", "evidence_version": "protocol-contract-v1",
        "selected_adapter": "openai",
    }
    # A content-bearing key.
    assert any(
        "content key" in f
        for f in _access_log_record_failures(
            {**base, "messages": [{"role": "user", "content": "hi"}]}
        )
    )
    # Same key spelled as a scalar body string is still a named content key.
    assert any(
        "content key" in f
        for f in _access_log_record_failures({**base, "body": '{"a":1}'})
    )
    # Prompt text smuggled through an otherwise-scalar field.
    assert any(
        "prompt content" in f
        for f in _access_log_record_failures({**base, "error_detail": "ping"})
    )
    # A dropped diagnostics field.
    missing = {k: v for k, v in base.items() if k != "endpoint_id"}
    assert any(
        "missing endpoint_id" in f
        for f in _access_log_record_failures(missing)
    )
