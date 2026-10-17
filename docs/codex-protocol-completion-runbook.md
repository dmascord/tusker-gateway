# Codex execution brief: finish PR #8 and prove compatibility end-to-end

You are Codex operating in `dmascord/tusker-gateway`. Continue branch
`feat/model-protocol-compatibility-20261017` (PR #8). Do not merge until
all exit criteria pass. This is an **execution assignment**, not a request
to write another design-only document. Inspect the current code before
changing it; prior PR work contains incomplete scaffolding and unrun tests.

## Mission

Deliver a stable OpenAI-compatible surface for OMP, OpenCode, Cline and
ordinary OpenAI SDK clients, regardless of whether upstream models are
DeepSeek, GLM/Z.AI, Qwen, Kimi/Moonshot, MiniMax, MiMo, OpenAI/APIM,
Gemini, Anthropic/LiteLLM, Mistral or unknown third-party hosted models.
Use real evidence, not family-name guesses. Preserve separate reasoning,
content, tool calls, opaque signatures, finish reasons and tool-result
continuations across streaming, nonstreaming, Responses and failover.

## Phase 0 — establish a reproducible baseline

1. Fetch PR #8 and current main, inspect the diff, open issues, CI and
   deployment manifests. Check repository instructions and existing replay
   engine APIs; **do not invent replay commands or endpoints**.
2. Create a baseline report with commit SHA, current cluster/deployment
   versions, test commands and failing tests. Verify the working tree is clean.
3. Run `python -m pip install -e '.[dev]'`,
   `python -m ruff check tusker_gateway/protocol_*.py`,
   `python -m pytest -q tests/test_protocol_*.py tests/test_omp_reasoning_contract.py`,
   then the entire repository suite. Fix failures before expanding scope.
4. Check `protocol_compat.py`, `protocol_runtime.py`,
   `protocol_evidence.py`, `protocol_signatures.py`, probe modules,
   `passthrough.py`, `endpoints.py`, Responses adapters and replay code.
   Confirm that each added helper is actually used at runtime.

## Phase 1 — complete implementation

1. Integrate a secure, authenticated **admin-only** probe entry point with
   the existing gateway authorization model. Bind executors to configured
   upstream endpoints and their existing credentials; no arbitrary URL,
   user-controlled host, secret logging or pool fallback. Require explicit
   operator approval for billable probes. Add distributed concurrency,
   endpoint cooldown, request budget and cancellation/timeout handling.
2. Implement durable, versioned behavioural fingerprints scoped to
   provider + endpoint + API transport + model + probe version. Separate
   passed/unsupported/unknown/unavailable; expire evidence and provide
   auditable overrides with precedence and rollback. Do not turn a generic
   200 response into proof that reasoning parameters are honoured.
3. Make verified features affect real routing/normalization consistently,
   for each candidate and transport: Chat Completions streaming/nonstreaming,
   Responses streaming/nonstreaming, retries and failover. Do not let
   per-provider adaptations mutate shared original conversation history.
4. Preserve provider-specific opaque reasoning/signature fields for
   same-provider continuation; never synthesize signatures. On cross-provider
   failover, translate only verified portable semantics. Preserve tool-call
   IDs and matching tool results; reject/avoid unsafe partial tool replay.
5. Complete protocol probes for structured tools, finish reasons, fragmented
   SSE, reasoning channels, two-turn reasoning replay, parameter dialects,
   Responses function-call/output continuation, and cancellation/errors.
   Validate returned tool function name, exact arguments, IDs and actual
   continuation; ensure invalid outputs cannot create false positives.
6. Support explicit safe overrides and operator diagnostics: selected
   provider/model/endpoint, evidence version, selected adapter, fallback
   reason, capability confidence and redacted probe outcome. No prompt,
   thinking text, tool arguments, tokens or credentials in telemetry.

## Phase 2 — replay engine as the primary regression gate

1. Locate and run the repository's existing replay engine. If absent,
   implement a deterministic replay harness using the gateway's actual
   HTTP/SSE interfaces, with captured **sanitized** fixtures and a
   provider simulator. Document the invocation commands.
2. Capture representative golden sessions from OMP, OpenCode, Cline and
   plain OpenAI SDK clients, after removing secrets, PII and reasoning text.
   Record protocol structure and event ordering, not private content.
3. Cover: normal text, reasoning-only chunks, mixed reasoning/content,
   empty content, native tool calls, duplicate DSML/XML markup, split JSON
   arguments, multiple parallel tool calls, missing finish reason,
   cancellation, retry, fallback, malformed upstream events, rate limits,
   5xx, tool-result continuation and Responses call/output pairing.
4. Run each fixture through the gateway with and without streaming, and
   with one upstream plus failover across two different providers.
   Compare semantic outputs against golden contracts. Assert exactly-once
   tool execution **at the client boundary**, correct IDs, no leaked
   reasoning-to-content, no dropped opaque same-provider signatures, and
   no duplicate tool results after retry.
5. Use a matrix of native and routed DeepSeek, GLM, Qwen, Kimi, MiniMax,
   MiMo, OpenAI, Gemini, Anthropic and Mistral fixtures. Mark unavailable
   live endpoints as NOT TESTED rather than PASSED.
6. Add positive, negative, property-based/fuzz, concurrency and regression
   tests. Gate CI on deterministic replay; produce machine-readable
   per-provider/per-feature results and minimal sanitized failure artifacts.

## Phase 3 — deploy, verify live, iterate

1. Inspect existing Kubernetes manifests, namespaces, credentials,
   rollout controls and current production topology. Prefer an isolated
   staging deployment with the same gateway config. Do not assume cluster
   access or create new credentials.
2. Build and tag an immutable image for the PR commit. Deploy to staging
   using the repo's existing deployment mechanism; capture image digest,
   configuration version and rollout status.
3. Run health checks, baseline traffic smoke tests, all replay scenarios,
   then **approved**, bounded live probes against configured upstream
   endpoints. Use the real OMP, OpenCode and Cline clients where available.
4. Exercise streaming/nonstreaming, tool execution, reasoning replay,
   Responses API, error handling and cross-provider failover. Verify
   observability, no secrets in logs, stable latency, error rates and
   resource usage. Compare against main/staging baseline.
5. For every failure: capture a redacted reproducer, write a regression
   test, fix the root cause, rebuild, redeploy to staging and rerun the
   full matrix. Repeat until all required checks pass, not just one smoke
   test. Never hide failures by weakening assertions.
6. Only after staging passes, prepare a canary plan with rollback image
   and explicit success/error thresholds. **Do not deploy to production
   without operator authorization**. If authorized, canary, monitor and
   promote gradually; roll back immediately on contract regressions.

## Exit criteria — all required

- All CI checks, full unit/integration suite and deterministic replay matrix
  green at the exact final commit SHA.
- Authenticated, authorized probe workflow exercised with negative access
  tests, endpoint allowlist and bounded cost/timeout checks.
- Every supported model/provider/transport pair has documented live evidence
  or is explicitly marked unverified/unsupported and safely handled.
- OMP, OpenCode, Cline and SDK client end-to-end tool and reasoning sessions
  verified; no duplicate execution or reasoning leakage.
- Staging deployment verified with image digest and recorded rollback.
- Security review: SSRF, credentials, replay privacy, prompt/tool logs,
  signature isolation, admin auth, denial-of-service and failover.
- PR includes a final compatibility matrix, test commands, CI links,
  staging deployment details, known limitations and rollout/rollback plan.
- PR stays draft until all mandatory checks pass. Do not claim completion
  when a tool, credential, replay engine or cluster is unavailable; report
  the exact blocker and continue independent work.

## Reporting cadence

Commit logically separated fixes to the PR branch. After each iteration,
post: changed files/commits, failing/passing tests, replay matrix delta,
live probe outcomes, staging image digest, unresolved blockers and next
actions. Prefer fixing defects over generating more speculative scaffolding.
