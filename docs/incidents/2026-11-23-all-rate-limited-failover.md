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

Final source `2a2d41aa9532fdc1596f95610015ae24aa63fc79`, image
`sha256:5af85cdc00ea896afed9169af61ecc7e82c66ce0f6644b7abe1f415011281864`;
provenance OK, smoke 200/200. The original request ID was not present in
current pod logs (pods had rotated), so the fix is based on the error shape,
sibling request traces, and current provider state.
