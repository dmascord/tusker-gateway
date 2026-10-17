# Provider/model protocol compatibility

## Contract exposed to clients

The gateway exposes OpenAI-compatible Chat Completions to OMP, OpenCode,
Cline and other clients. Provider-specific model families are **not** a
reliable indication of an upstream wire dialect. Keep these boundaries:

- Ordinary answer text: `message.content` / `delta.content`.
- Thinking text: `reasoning_content` / `delta.reasoning_content`.
- Executable calls: `tool_calls` with stable IDs, function names and JSON
  argument strings. Do not interpret thinking text as executable output.
- Tool results: `role=tool` with `tool_call_id` for Chat Completions;
  `function_call_output` with `call_id` for Responses.
- Streaming: finish_reason is a separate terminal event; native structured
  tool calls win over duplicate text markup.
- A tool result is **not** proof that execution succeeded.

## Compatibility resolution

1. Resolve the actual upstream provider/model **for each candidate attempt**.
2. Use provider-scoped native adaptations, never model-name guesses alone.
3. Consult fresh, endpoint-scoped evidence for optional features. Stale,
   unknown and temporarily unavailable evidence must not be treated as
   unsupported or passed.
4. Apply explicit operator overrides only after validating the endpoint
   identity and documenting the reason.
5. Preserve replay-sensitive reasoning and provider signatures where
   required; do not invent signatures or reconstruct encrypted thinking.
6. On failover, build a new upstream body from the original messages rather
   than carrying another provider's mutations forward.

## Probe matrix

The following checks require actual upstream execution. The pure classifiers
in `protocol_probe.py` only evaluate responses; they **do not run probes**.

| Feature | Positive proof | Failure classification |
| --- | --- | --- |
| structured_tool_calls | Nonempty native calls with IDs and parseable JSON args | Malformed/absent -> unknown |
| tool_call_finish | Valid calls and `finish_reason=tool_calls` | `stop` -> unknown (client may compensate) |
| reasoning_channel | Separate nonempty `reasoning_content` | Absent -> unknown |
| reasoning_replay | Successful second turn replaying exact required thinking | One-turn probe -> unknown |
| reasoning_effort | Controlled request accepted with intended effort | 4xx requires structured error review |
| thinking_parameter | Native provider accepts documented parameter | 4xx requires structured error review |
| streaming_tool_fragments | Assembled stream with valid ID, name, JSON arguments and terminal | Incomplete -> unknown |
| responses_function_calls | Responses `function_call` followed by matching output | Chat-only probe -> unknown |

Never store prompts, arguments, reasoning, tool outputs or credentials in
protocol evidence. Version probes when semantics change. Use distinct endpoint
IDs for deployments that share provider and model names.

## Provider families to cover with recorded fixtures

- DeepSeek R1/V3/V4: native vs routed, DSML, thinking replay, fragmented calls.
- GLM / Z.AI: native thinking controls vs routed OpenAI-style settings.
- Qwen: Hermes/Qwen function markup, inline think blocks, tool finish.
- Kimi / Moonshot: reasoning channel and replay requirements by endpoint.
- MiniMax / MiMo: inline thinking and native structured calls.
- OpenAI/APIM/Codex: effort, completion tokens, Responses tool outputs.
- Anthropic/LiteLLM: signed thinking and encrypted replay preservation.
- Gemini: thought signatures on tool continuations.
- Mistral: typed reasoning content parts.

## Current rollout status

This PR introduces conservative profiles, copy-on-write replay adaptation,
a versioned evidence store and pure probe classifiers. It does **not** yet
provide automatic active probing, signed-reasoning translation for every
provider, or proven compatibility across every deployment. Leave it draft
until integration fixtures, test runs and representative provider smoke tests
are complete.


## Deterministic replay matrix (regression gate)

`tusker_gateway/tools/protocol_replay_matrix.py` drives the gateway's actual
HTTP/SSE surface against a scripted provider simulator — a live loopback
aiohttp upstream that serves hand-built frames — so every scenario exercises
the real request normalization, stream normalization, tool-call assembly,
error classification and cross-provider failover paths. Fixtures contain
protocol structure only (field names, ordering, finish reasons), never
prompts, reasoning text, credentials or PII.

```bash
# Human-readable table; exit 1 if any scenario fails
python3 -m tusker_gateway.tools.protocol_replay_matrix

# Machine-readable per-scenario results
python3 -m tusker_gateway.tools.protocol_replay_matrix --json

# Skip the cross-provider failover sweep
python3 -m tusker_gateway.tools.protocol_replay_matrix --no-failover
```

`tests/test_protocol_replay_matrix.py` runs the same matrix as a CI gate and
pins the scenario set, so a refactor that drops a case fails the suite rather
than silently shrinking it. Current coverage: plain text, empty content,
reasoning-only, mixed reasoning/content, reasoning aliases, native tool calls
(stream and non-stream), split JSON arguments, parallel tool calls, missing
finish reason, tool-result continuation (assistant `tool_calls` + `tool` role
result), duplicate DSML markup, malformed SSE frames, upstream 4xx/5xx,
mid-stream close, an operator-diagnostics check, a Responses
`function_call`/`function_call_output` pairing check, a **client
cancellation** check (a client that disconnects mid-stream must not hang the
loop or break the next request) and a **transient retry recovery** check (a
503 fails that request but must not poison the route).

Every success-path fixture then runs a second time under **cross-provider
failover** (alpha returns 503, beta serves the same contract), which is the
runbook's Phase 2.4 requirement; the three failure-shaped fixtures are
excluded because a clean fallback would contradict their contract. That is
17 scripted scenarios + 4 route/diagnostics/lifecycle checks + 14 failover
replays + 15 golden-fixture replays = 50 matrix rows (`50/50 scenarios passed`).

Two gateway defects this harness caught (fixed, with regression tests in
`tests/test_fault_scenarios.py` and `tests/test_tool_call_stripper.py`):

- A truncated upstream SSE body surfaces as `aiohttp.ClientPayloadError`,
  which was absent from the wrap set at `diagnostic_raw_stream`. The
  documented `stream_closed_prematurely` envelope (AGENTS.md, 2026-10-14
  00:17Z case) was therefore unreachable for its own production shape.
  `stream_closed_prematurely` is also a declared pre-visible recovery code,
  preserving the failover that `ClientError` membership previously provided.
- An id-less continuation delta for an already-identified tool call received
  a fresh synthesized id (`call_stream_0_N`), so a client assembling by
  (choice, index) saw one call with two ids. The opening id is now reused.


## Golden session capture (real client shapes)

`tusker_gateway/tools/capture_golden_sessions.py` records wire shapes from a
*real* client, not a hand-written sketch. It raises a loopback proxy that
scripts a tool-calling turn, so a cooperating agent CLI runs the tool and
sends back the `assistant.tool_calls` + `tool` result — the continuation
shape the replay matrix cares about — which is then sanitized before it is
written.

```bash
# Terminal 1: proxy + sanitizer (stops itself once the continuation lands)
python3 -m tusker_gateway.tools.capture_golden_sessions \
    --client opencode --tool-name glob

# Terminal 2: point a sandboxed client at the printed base URL
cd /tmp/capture/oc          # holds an opencode.json naming the proxy baseURL
opencode run --standalone --model capture/capture-model "list the files here"

# Re-validate the committed fixture without capturing
python3 -m tusker_gateway.tools.capture_golden_sessions --check
```

Sanitization is by construction: credential, cookie and session *header
values* are dropped (names are kept, since they are part of the contract),
prompt and tool-argument *text* becomes `<redacted>`, reasoning deltas become
a placeholder (so their ordering is preserved without the text), and absolute
paths, hostnames, emails and long hex runs are replaced. `--check` re-scans
every committed fixture and exits non-zero if private content reappears.

Captured in `tests/fixtures/golden/`:

|Client|Fixture|Status|
|---|---|---|
|OpenCode 2.0.30|`opencode.json`|Captured: preflight (no tools), agent turn, and the tool-result continuation (`system → user → assistant(tool_calls) → tool`)|
|OMP|`tests/fixtures/hermes_code_turn.json`|Pre-existing OMP-shaped turn (2026-09-25), replayed by `tests/test_fault_scenarios.py`|
|Cline|—|**NOT TESTED** — no Cline binary on this workstation|
|plain OpenAI SDK|`openai-sdk.json`|Captured: initial tool declaration and the tool-result continuation (`user → assistant(tool_calls) → tool`). The driver is a ~40-line script described in the tool docstring (`python3 -m tusker_gateway.tools.capture_golden_sessions`); no SDK driver is committed.|

`tests/test_golden_capture.py` asserts the captured fixture still carries the
continuation shape (assistant `tool_calls` plus a `tool` role with a
`tool_call_id`) and that the privacy validator passes, so neither coverage nor
sanitization can silently regress.

`tusker_gateway/tools/golden_replay.py` replays every captured request through
the gateway — **streaming, non-streaming, and under cross-provider failover** —
so the fixtures are a gate rather than an archive. Its rows appear in the
matrix as `golden/<client>[<index>]/<mode>` (15 rows for the five captured
requests). Because the fixtures are sanitized, this is a shape test: the
reduced tool schemas are re-expanded to valid JSON Schema and redacted
argument strings are replaced with `{}`, so nothing private is reintroduced
while the role ordering, `tool_calls` block, `tool_call_id` linkage and
client-specific body keys (`store`, `stream_options`,
`max_completion_tokens`) are exercised exactly as the client sent them.


## Operator overrides and per-attempt diagnostics

Probes are the primary source of truth (`GET /admin/protocol/evidence`,
`POST /admin/protocol/probe`), but two operator-facing controls exist for
when a live probe is undesirable or unavailable.

**Declared verdicts** — `TUSKER_PROTOCOL_OVERRIDES_JSON` forces a feature
verdict for a route the operator has confirmed by hand:

```json
{"deepseek": {"reasoning_replay": "passed"},
 "zai/glm-4.6": {"thinking_parameter": "passed"}}
```

Provider keys apply to every model on that provider; `provider/model` keys
win. A declared `passed` is honoured **only when no probe evidence exists**
for the route: fresh evidence of any status outranks it, so an override can
never silence a failing probe. `unsupported` and unknown strings are not
treated as a pass.

**Per-attempt access-log fields** — every dispatch records, on the access
log record and therefore the request's span context:

|Field|Meaning|
|---|---|
|`protocol_family`|Resolver family hint (`deepseek`, `glm`, `qwen`, …)|
|`protocol_confidence`|`provider`, `heuristic`, or `unknown`|
|`endpoint_id`|Opaque fingerprint of the configured endpoint that served the attempt (hashes `base_url`/`chat_path`/`chat_api`; credentials excluded)|
|`evidence_version`|Probe-evidence contract label the resolver consulted (`protocol-contract-v1`)|
|`selected_adapter`|Transport that served the attempt: `http`, `OpenCodeCLIAdapter`, `ClaudeCodeCLIAdapter`, or `KiloCLIAdapter`|
|`fallback_reason`|`previous_attempt:<ExcType>` when the router moved on|

The fields are emitted on **both** dispatch shapes — a pooled attempt
(`_call_with_pool_fallback`'s candidate loop) and a direct provider-prefixed
route — so one access record is enough to reconstruct which upstream, which
transport and which evidence generation produced the verdict. The matrix's
`operator_diagnostics` row drives one request through each route and asserts
the full set is present and that no prompt or credential text leaks
alongside it.

These are computed from the resolved provider/model only. No prompt,
reasoning text, tool arguments, tokens or credentials reach the access log,
span, or evidence store — the store's schema has no column that could hold
them.
