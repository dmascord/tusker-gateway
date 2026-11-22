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

### Deployed

Source `87444f933c0ebcd31d602bd9daa3240e6de73d78`, image
`sha256:93f6da6ee7a4f9bc8c8f455964415bfba3691ec92154344a9ff0fcf06c7d3d0b`;
provenance OK, smoke 200/200, rate-limit branch confirmed live in the deployed
module. The original request ID was not present in current pod logs (pods had
rotated), so the fix is based on the error shape and current provider state.
