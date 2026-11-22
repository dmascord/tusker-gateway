## 2026-11-22 — Exhausted failover reported only the last candidate's timeout

### Symptom

`req_4ae4a2134158c498` (OMP, `hermes-privacy`, privacy pool, streamed, 13
tools, image input, ~208K prompt tokens) failed after 163.5s. The client saw:

> Upstream provider error: Provider candidate exceeded its per-attempt timeout. Request ID: req_4ae4a2134158c498.

That wording blamed a single 90s idle timeout and hid the real story: every
eligible privacy candidate had already failed, for several different reasons.

### Timeline (gateway access log, UTC)

| Time | Candidate | Outcome |
|---|---|---|
| 04:03:53 | `openai-codex/gpt-5.6-luna` | `[Errno 104] Connection reset by peer` at 29s |
| 04:04:22 | `mlx-mac/qwen3.8-27b` | `503 provider capacity unavailable` (queue wait expired) at 20s |
| 04:04:42 | `opencode-go/gpt-5.6-luna` | `[Errno 104] Connection reset by peer` at 25s |
| 04:05:07 | `opencode-go/gpt-6-luna` | 90s idle timeout — no SSE progress before first token |
| 04:06:37 | — | no eligible candidates (recovery + tool-compat probes found none); request failed |

The privacy pool only had four routes that satisfied image input + tools +
the 212K-token context + privacy-evidence/ZDR policy; once all four failed,
`request_excluded=4` left nothing to dispatch. This was upstream instability
plus a genuinely constrained pool, not a single stalled provider.

### Root cause

`_call_with_pool_fallback` kept only `last_error` and re-raised it when the
pool was exhausted. The terminal verdict therefore described whichever
candidate failed last (here, the 90s timeout) and discarded the earlier
resets and capacity rejection. The access log even carried contradictory
signals: `fallback_reason=previous_attempt:ClientOSError` while
`failure_class=provider_timeout`.

### Fix

1. `tusker_gateway/errors.py`: `ProviderFailoverError(ProviderError)` carries
   a client-safe summary of all candidate failures, e.g.
   `All 4 eligible provider candidates failed: connection reset x2, capacity unavailable, timeout`
   (code `all_candidates_failed`). Raw upstream bodies never enter the summary.
2. `tusker_gateway/endpoints.py`: `_call_with_pool_fallback` records each
   failed candidate and, at the terminal raise points, aggregates when at
   least two *distinct* candidates failed with *mixed* failure classes. A lone
   failure, a homogeneous run (e.g. every candidate rate-limited), or a last
   error with dedicated client guidance (`RateLimitError` with `Retry-After`,
   `ClaudeAuthRequiredError`) keeps its original response semantics.
3. `tests/test_chat.py`: regression tests for the mixed-failover aggregate and
   for preserving single-candidate errors.

### Verification

- `pytest tests/ --ignore=tests/test_passthrough_providers.py`: 2203 passed,
  8 skipped.
- Rendered client detail for this incident's failure mix is the aggregate
  summary above, not the single "per-attempt timeout" line.
- Deployed: commit `aa9671b75944d5fea63f974fca5111981bf62c6d`, image
  `registry.tusker.net.au:5000/tusker-gateway@sha256:35805813b1c621dd65204e3ac87b4ab96fa103ee3c1ac528cd125cb8f1a42535`,
  tag `swarm-alpine-aa9671b75944d5fea63f974fca5111981bf62c6d`.
  `k8s/deployment.yaml` is pinned to that digest; pod
  `tusker-gateway-cc6f5f6cd-fqhm7` runs it and `/health` reports the same
  commit.

### Not changed

The upstream resets and the `mlx-mac` capacity rejection are external
conditions; raising `opencode-go`'s 90s attempt budget or the `mlx-mac` queue
wait would only mask them and lengthen a doomed request. Those remain tuning
decisions, not defects in the failover logic.
