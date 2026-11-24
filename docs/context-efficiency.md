# Context efficiency — advisory headers, RTK honesty, document handling

This document records the token/context-efficiency surface of the gateway and
the evidence behind each decision. It exists because a third-party project
(`artificemachine/token-diet`) was evaluated as a possible source of
improvements, and the evaluation produced one genuinely missing capability
(context-pressure advisory), one broken path worth fixing (Anthropic document
blocks), and a warning about a feature the gateway already ships (RTK).

## 1. Context-pressure advisory headers

**Problem.** The gateway computes a prompt-token estimate for every request
(`context_limits.estimate_prompt_tokens`) and knows each route's context
window. Before this change the only way a client learned the prompt was
getting large was the hard `context_length_exceeded` failure
(`errors.py:ContextWindowExceededError`, HTTP 400). That failure costs a full
round-trip and leaves agent clients such as OMP in compaction recovery. There
was no "you are at 85% of the window" signal.

**Surface.** `tusker_gateway/context_advisory.py`, emitted on
`POST /v1/chat/completions` (JSON and SSE), `POST /v1/responses`, and
`POST /v1/messages`:

| Header | Meaning |
|---|---|
| `X-Tusker-Context-Estimate` | Estimated prompt tokens for this request. |
| `X-Tusker-Context-Window` | Declared/learned context window of the serving route. |
| `X-Tusker-Context-Remaining` | `window - estimate`. |
| `X-Tusker-Context-Used-Pct` | `estimate / window`, integer percent. |
| `X-Tusker-Context-Warning` | Present only at/above the advisory threshold. |

The estimate is always emitted when it can be computed. The window triplet is
emitted only when the route's window is actually known — the gateway does not
fabricate a ceiling for a route whose window it cannot resolve.

**Advisory only.** No routing, pricing, token accounting or request-body
behaviour changes. A route that cannot fit the prompt is still skipped during
pool selection exactly as before.

**Configuration.**

| Variable | Default | Meaning |
|---|---|---|
| `TUSKER_CONTEXT_ADVISORY` | `1` | `0`/`false`/`no`/`off` disables all emission. |
| `TUSKER_CONTEXT_ADVISORY_PCT` | `80` | Percentage at/above which `X-Tusker-Context-Warning` is emitted. |

## 2. Anthropic document blocks are rejected, not stringified

**Problem.** `translators/anthropic.py` converted an Anthropic `document`
content block (PDF and other document sources) by JSON-stringifying the whole
block into a text part. A 1.5 MB PDF therefore became roughly **468,788
estimated prompt tokens** of escaped base64 shipped as prose. For a
non-Anthropic upstream the payload was unprocessable; for an Anthropic-shaped
upstream it still consumed the entire context window.

The OpenAI Chat Completions request shape has no document part, the gateway
image ships no PDF parser, and no configured upstream route accepts a
document natively — so "preserve the block structure" had no valid target.

**Fix.** Fail closed with an actionable error:

```
unsupported_document_block: Anthropic document blocks are not supported by
this gateway: the OpenAI Chat Completions request shape has no document part
and no upstream route parses PDFs. Extract the document to text client-side
and send it as a text block.
```

The failure is a 400 before any upstream call, so the caller's budget is not
spent on bytes no model can read.

**Not implemented (deliberately).** Client-side text extraction of PDFs
(`docextract`-style interception) belongs in the agent harness, not the
gateway: the gateway has no filesystem view of the client's project and
adding a PDF parser to the runtime image is a dependency decision this change
does not make.

## 3. RTK: kept, but with honest semantics

The gateway ships an RTK-style tool-output compressor
(`tusker_gateway/rtk/`), enabled in production via `TUSKER_RTK_ENABLED=true`
(`k8s/deployment.yaml`). The third-party evaluation produced **negative
independent evidence** about this class of tool, which is recorded here so the
decision is not silently reversed.

### The evidence

`artificemachine/token-diet` removed RTK from its own stack after independent
benchmarks:

- **Quesma** (2026-09, Terminal-Bench 2.1, 1,740 attempts, ~$1,500 spend):
  the reported savings did not translate into lower cost. Claude Code/Fable 5:
  −5% total but **+1% per task**; OpenCode/DeepSeek V4: **+17% per task**.
  Verdict: *"We do not recommend RTK as a generic cost-saving tool."*
- **JetBrains SkillsBench**: no savings, extra turns at low effort.

Two structural reasons: the `rtk gain` metric counts removed output **bytes**
divided by four, not billed tokens (one comparison credited 120.5M "saved"
tokens to two `head -1` calls that would never have returned the whole file);
and terminal output is only ~7–11% of a frontier model's input, so compression
that costs one extra turn loses money.

### What that means for this gateway

- `tusker_rtk_bytes_saved_total` is **not** a savings metric. Its help text
  now says so explicitly: *"Tool-output bytes removed by RTK compression (NOT
  billed-token savings)"*. Billed impact must be read from
  `tusker_tokens_total`.
- A real A/B on billed tokens (not bytes) is required before any RTK savings
  claim is published. The gateway's shadow-divergence infrastructure
  (`TUSKER_VALUE_ROUTING=shadow`) is the natural place to run it.

### Hardening applied

| Change | Before | After |
|---|---|---|
| pytest filter | kept only summary + test IDs; the `E AssertionError: ...` line and the `- reason` suffix were dropped, forcing a second test run | keeps the assertion source line, the `E` explanation and the failure reason |
| cargo filter | dropped everything after ` ... ` | keeps the panic message |
| block-size cap | 64 KiB — a 270 KB `git diff` passed through untouched | 512 KiB; only `role == "tool"` content ever reaches the filter, so the cap bounds pathological payloads rather than protecting system prompts |
| `X-Rtk-Token-Saver: off` | documented in the module docstring, never read | actually read on `/v1/chat/completions` and `/v1/messages`; compression is skipped for that request |
| metric catalogue | `tusker_rtk_blocks_total` was defined but undocumented | documented in the catalogue |

The pytest change is the one with direct cost impact: dropping the assertion
reason forces the model to re-run the test, which is exactly the "extra turn
costs more than the compression saves" failure mode the benchmarks describe.

## 4. Token visibility on the dashboard

`GET /dashboard` previously showed cache hit/miss ratios but no token
counters, even though `tusker_tokens_total{pool,provider,model,direction}`
existed. The "Cache & Budgets" card now carries a **tokens (billed,
upstream-reported)** tile with the prompt/completion split, summed across all
series via the new `Counter.sum_where(**labels)` helper.

## 5. Method for any future savings claim

Per the benchmark methodology the third-party project settled on, a publishable
claim needs: the model and harness, the task set, the number of runs, and a
cost basis in money or billed tokens. A compression ratio measured on stdout is
not that. This applies to any future "token-diet"-style claim made about this
gateway.

## Verification

- `tests/test_token_diet_hardening.py` — regression coverage for every item
  above. 11 of its 19 tests fail against the unfixed tree (verified in a
  scratch worktree at `HEAD`).
- `tests/test_rtk.py`, `tests/test_rtk_metrics.py` — updated where they pinned
  the old lossy contract (64 KiB cap, dropped assertion reason).
