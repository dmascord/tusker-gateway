# PR #8 baseline report — 2026-10-17

## Scope
- Branch: `feat/model-protocol-compatibility-20261017` (PR #8), 47 commits ahead of `main` (935440d).
- Working tree clean at inspection; baseline commit after fixture fix: see git log.

## Baseline evidence (Phase 0)
- `python -m ruff check tusker_gateway/protocol_*.py` → All checks passed.
- `python -m pytest -q tests/test_protocol_*.py tests/test_omp_reasoning_contract.py`
  → initially 2 failed (`test_protocol_probe.py` fixtures used `{"query":"hello"}`
  arguments while the probe runner contract (protocol_probe_runner._TOOL +
  classifier `_echo_arguments_valid`) requires echo `{"value":"ok"}`).
  Fix: fixtures aligned with the runner contract + added stricter negative case
  (valid JSON, wrong echo → `unknown`). After fix: **42 passed**.
- Full suite `python -m pytest -q tests/ --ignore=tests/test_passthrough_providers.py`
  → 1 failed, 1743 passed, 8 skipped: `test_tool_call_stripper.py::test_stream_normalizer_promotes_reasoning_content`.
- Failure root: main promotes `reasoning_content`→`content` when content is null;
  the PR (per runbook mission "preserve separate reasoning/content") stops the
  duplication and normalizes reasoning aliases (`reasoning`, `reasoning_text`)
  into `reasoning_content`. Verified against the actual OMP client (pi-coding-agent
  17.4.0, dist/cli.js line ~1031): the OpenAI-completions stream parser reads
  `["reasoning_content","reasoning","reasoning_text"]` from each delta, emits
  thinking blocks, ends turns on `finish_reason` (not on empty content), and
  replays `reasoning_content` on assistant turns for DeepSeek-class tool
  continuation. Conclusion: separate-channel behavior is required for OMP;
  duplication is a visible-thinking regression. Old test premise is stale →
  updated to the new contract with equal-or-stronger assertions.

## Module wiring audit (dead-code check)
Wired at runtime:
- `protocol_compat.adapt_upstream_chat` → `passthrough.py` request build path (verified-features gated).
- `protocol_runtime.verified_features_for_endpoint` → same call site; opt-in via
  `TUSKER_PROTOCOL_EVIDENCE_ENABLED`, fail-closed, LRU-cached store.
- `protocol_evidence.ProtocolEvidenceStore` → runtime store (Postgres/SQLite).
- Stream normalizer alias normalization → `endpoints.py` `_normalize_stream`/`_normalize_stream_legacy`.

Reachable by callers (no separate runtime caller; invoked by the admin probe
surface built in Phase 1, which passes the resolved endpoint in explicitly):
- `protocol_probe_runner.probe_chat_contract`
- `protocol_replay_probe.probe_reasoning_replay`
- `protocol_stream_probe.probe_streaming_tool_fragments`
- `protocol_responses_roundtrip.probe_responses_roundtrip`
- `protocol_responses_probe.classify_responses_function_call` (used by roundtrip)
- `protocol_parameter_probe.probe_parameter`
- `protocol_probe_coordinator.ProbeCoordinator`
- `protocol_signatures.strip_cross_provider_replay` (called from the failover path)

## Phase 1 work items derived from audit
1. Admin-only probe HTTP surface: `/admin/protocol/probe` (session/Bearer auth via
   existing admin middleware), operator approval gate, configured-endpoint
   allowlist (protocol_probe_policy), concurrency+cooldown (ProbeCoordinator),
   bounded timeout/budget, evidence recording. Executors bound to the selected
   configured endpoint with its existing auth strategy; no pool fallback.
2. Wire `protocol_parameter_probe` into the same surface (reasoning_effort,
   thinking dialect) — currently orphaned.
3. Cross-provider failover: apply `strip_cross_provider_replay` to assistant
   history when the selected candidate upstream provider differs from the one
   that produced stored assistant messages; keep same-provider opaque fields.
4. Operator diagnostics: `/admin/protocol/evidence` read-only view (no payloads).
