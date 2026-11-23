## 2026-11-23 — All-candidates-rate-limited failover rendered as a generic 502

### Symptom

`req_1be799418c9eae87` returned:
"Upstream provider error: All 12 eligible provider candidates failed: rate limited x12. Request ID: req_1be799418c9eae87."

The text is accurate but the verdict is wrong: the condition is uniform quota
exhaustion, which should be a 429 the client can back off from, not a generic
`provider_error` (502) with no `Retry-After`.

### Root cause

`_terminal_error()` in `_call_with_pool_fallback` returned `ProviderFailoverError`
for any mixed-candidate aggregation. `ProviderFailoverError` is a `ProviderError`
(status 502, code `all_candidates_failed`), so the existing rate-limit rendering
(`RateLimitError` → 429 + `Retry-After`) was bypassed. Individual candidates
had already supplied usable 429 windows, but they were discarded.

Contributing live state: at investigation time 9 of 11 code-pool providers had
provider-level cooldowns and zero selectable candidates (alibaba, commandcode,
github-copilot, GHE, kilo, minimax, ollama-cloud, zai), leaving only google and
opencode-go with selectable routes. A 12-candidate chain can therefore plausibly
be all rate-limited.

### Fix

- `endpoints.py`: `_record_failure` captures the per-candidate `retry_after`
  from `RateLimitError` (via `_retry_after_seconds_for_exc`). `_terminal_error`
  now returns a `RateLimitError` carrying the **shortest** observed window when
  every recorded failure is rate-limit class; mixed failure classes still get
  `ProviderFailoverError`.
- `errors.py`: `ProviderFailoverError` annotation widened to `dict[str, Any]`.
- `tests/test_chat.py`: regression proving a two-candidate all-429 failover
  raises `RateLimitError(code="rate_limit_exceeded", status=429)` with
  `Retry-After` equal to the shorter of two supplied windows (30s vs 90s).

Full offline suite: 2218 passed, 8 skipped.

### Follow-up: the 12-attempt cap itself

Log forensics on sibling requests showed the deeper cause: 10 of 12 attempts
were spent on quota-exhausted `google` routes, and healthy providers
(`synthetic`, `openrouter`, `opencode-cli`, `openai-codex`) further down the
ranking were never reached. Two ideas were rejected:
- recording 429s as permanent quality failures — a 429 is transient capacity
  and must not demote a route after its quota window resets;
- excluding a provider after its first 429 — `google` quotas are model-scoped,
  and cycling through every model is intentional.

Instead, rate-limit failures no longer consume the bounded **viable-attempt**
budget: while every failure in the chain is a 429, the failover keeps walking
the ranked list. A hard ceiling of 3× the configured attempts bounds total
upstream dispatches. Quality scores and cooldown behavior are untouched —
cooldown routes are still filtered out before ranking and re-enter when their
window expires.

Regression test: with `TUSKER_MAX_PROVIDER_ATTEMPTS=2`, two 429s followed by a
healthy candidate still reach the third dispatch.

### Deployed

Final source `f65cf8fd7f1cc53fdc3cbcd59a24e52f3cf3888c`, image
`sha256:bc807cf71ee200979f28715b0d0f093c3c917988de3d71c3de9e2c30087dfcfa`;
provenance OK, smoke 200/200. The original request ID was not present in
current pod logs (pods had rotated), so the fix is based on the error shape,
sibling request traces, and current provider state.

### Post-deploy measurement and final correction

Over 168 chat requests after the first deploy: 164 × 200, 4 × 502 (97.6%
success). 429s now surfaced as actionable 429s ("retry in about 60s", 3600s,
etc.). However the cap extension never engaged: every remaining
`all_candidates_failed` chain contained 11 429s **plus one non-429 failure**
(e.g. `req_53e75b4f75fe429e`: 11 Google quota 429s + one synthetic
provider_error), so the "all failures were 429" precondition was false.

Final correction: refund the attempt slot on **every** 429 regardless of the
other failure classes; only the 3× hard dispatch ceiling bounds total work.
Regression test updated to the mixed case (429 + provider_error + healthy
candidate with `TUSKER_MAX_PROVIDER_ATTEMPTS=2`). Suite 2219 passed; deployed
and confirmed live.
