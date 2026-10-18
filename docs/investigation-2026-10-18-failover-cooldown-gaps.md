# Fail-fast and cooldown gaps in the pool fallback path

Scope: does the gateway ever sit on a model that keeps returning 4xx/5xx,
waiting 30–90s per attempt, and does a repeatedly-failing route cool down
properly. Read against production config (`k8s/deployment.yaml`, live
deployment `hermes/tusker-gateway`).

## Verdict

Three real gaps, one non-gap. All three are fixed; see "Fixes implemented".

| # | Scenario | Symptom | Severity | State |
|---|---|---|---|---|
| 1 | 5xx on every request for a long-running outage | breaker re-probed every 60s indefinitely, no escalation by consecutive-failure count | medium | fixed |
| 2 | A request with hung candidates | up to 4 minutes per request; one stalled candidate costs up to 240s under the per-attempt overrides | high | fixed |
| 3 | Inter-attempt delay | none — the loop fails over immediately | non-gap | — |
| 4 | A provider 4xx surfacing as a raw (non-`GatewayError`) exception | cooldown silently downgraded to the 60s policy window | medium | fixed |

## Empirical classification

Probing `_cooldown_seconds_for_provider_error` with a synthetic
`ProviderError`-shaped exception (Python 3.12, no env overrides):

| status | body | result |
|---|---|---|
| 400 | empty | 3600s |
| 400 | `too many tokens` | 3600s |
| 405 | empty | 3600s |
| 409 | empty | 3600s |
| 413 | empty | 3600s |
| 422 | empty | 3600s |
| 404 | empty | 3600s |
| 401 | empty | 3600s |
| 500 | empty | None (60s policy) |
| 502 | empty | None (60s policy) |
| 503 | empty | None (60s policy) |

The "all other 4xx → 3600s" fall-through is good, but the 5xx arm has no
escalating backoff and no quota-wording path; 5xx is structurally the
weakest case.
## Non-gap: fail-fast between candidates

The fallback loop has no sleep, backoff, or retry of the same candidate.
`endpoints.py` `_pool_fallback_loop` walks `pool_manager.select(...)` and
moves to the next candidate as soon as the attempt raises. A fast 4xx/5xx
does not delay failover; the client-visible cost of a dead candidate is the
failure round trip only.

## Gap 2 (highest): the budget is per candidate, not per request

`_provider_attempt_timeout_secs` returns `TUSKER_PROVIDER_ATTEMPT_TIMEOUT_SECS`
(default **45s**), clamped only by the request deadline when one exists:

```python
configured = max(0.1, float(os.environ.get("TUSKER_PROVIDER_ATTEMPT_TIMEOUT_SECS", "45")))
...
remaining = max(0.0, deadline_at - asyncio.get_running_loop().time())
return max(0.0, min(configured, remaining - 0.25))
```

In production, `TUSKER_REQUEST_TIMEOUT_MS=240000` (4 minutes) is set, so the
hard outer bound is the request deadline. Per-attempt overrides
(`TUSKER_PROVIDER_ATTEMPT_TIMEOUT_OVERRIDES_JSON`) raise the per-candidate
budget to 240s for `local-llm`/`mlx-mac`, 180s for `ollama-cloud` and
`synthetic`, 120s for `kilo-cli` and `alibaba/qwen3.8-max`, and 90s for
`zai` and `opencode-go`. A request whose first candidate is an `ollama-cloud`
stall has up to 180s to fail before the next candidate starts, so a couple of
slow overrides plus one stalling upstream can absorb the whole request
deadline before the loop reaches its remaining candidates.

The per-candidate budget is the only knob. There is no aggregate
"attempts must finish within N seconds" setting, so the number of candidates
that actually get tried depends on how each one fails: fast 4xx/5xx failures
walk the full six-candidate budget, stalls consume it in one or two.

Evidence: `endpoints.py:4036-4068` (`_provider_attempt_timeout_secs`),
`endpoints.py:5063-5075` (attempt/max_attempts logging),
`_max_pool_provider_attempts` (`TUSKER_MAX_PROVIDER_ATTEMPTS`, default 6),
`test_chat.py::test_pool_fallback_bounded_per_attempt_timeout` (asserts the
per-attempt bound, not a total bound).

## Gap 1: 5xx never escalates and never marks the route

`status >= 500` returns `None` from `_cooldown_seconds_for_provider_error`,
which means the breaker policy cooldown — 60s in production
(`TUSKER_CIRCUIT_COOLDOWN` unset). Nothing else escalates with failure count:

- The breaker reopens for the same 60s after every failed HALF_OPEN probe, so
  a route that has failed 500 times is re-probed exactly as often as one that
  failed five times.
- `_mark_permanently_failed` explicitly excludes 5xx ("Transient 5xx / 429 are
  NOT marked"), so the auto-free pool keeps the route in rotation.
- The window-ratio trip condition is evaluated over a rolling 20-request
  window that any single success resets.

The design intent is defensible — a genuinely transient 5xx recovers within
60s and a longer cooldown would keep a healthy model out. The gap is that
*non-transient* and *transient* 5xx are indistinguishable at every failure
count.

Note the asymmetry with 4xx, which is the one part of this path that is
tighter than it first looks: an unnamed 4xx (400/405/409/413/422) already
returns the 3600s permanent bucket from
`_cooldown_seconds_for_provider_error`, because only `>= 500` short-circuits
to `None`. So a route returning 400 gets a 1-hour cooldown, while the same
route returning 503 gets 60s forever.

One caveat on how the 3600s reaches the breaker: `record_failure` stores the
cooldown override only on the row it writes, and `_cooldown_for_exc` returns
`None` for a non-`GatewayError` exception. A provider failure that escapes as
a raw exception (rather than a `GatewayError` subclass) reaches
`breaker.record_failure(..., cooldown_secs=None)` and therefore gets the 60s
policy cooldown even when its status is 400. That path downgrades exactly the
4xx cooldown the classification table computes.

## Production values in play

From the live deployment (`kubectl -n hermes get deployment tusker-gateway`):

- `TUSKER_CIRCUIT_ENABLED=true` — the breaker is live, so all of this applies.
- `TUSKER_CIRCUIT_COOLDOWN` unset → 60s policy cooldown.
- `TUSKER_CIRCUIT_CONSECUTIVE` unset → trips at 5 consecutive failures.
- `TUSKER_MAX_PROVIDER_ATTEMPTS` unset → 6.
- `TUSKER_PROVIDER_ATTEMPT_TIMEOUT_SECS` unset → 45s.
- `TUSKER_RETRY_PERMANENT_COOLDOWN` unset → 3600s.
- `TUSKER_PROVIDER_CAPACITY_COOLDOWN_SECS=300` — capacity group cooldowns are
  the one path with an explicit non-default window.

## Fixes implemented

All three were fixed in this session; the sections above describe the
behaviour that was corrected.

1. **Escalating transient cooldown** (`cooldown.py`). New
   `transient_cooldown_secs(consecutive_failures)` and
   `DEFAULT_TRANSIENT_ESCALATION_SECS = (60, 300, 3600)`. The first
   `TRANSIENT_ESCALATE_AFTER = 3` consecutive failures keep the breaker
   policy cooldown so a brief blip still recovers fast; after that the
   window grows along the ladder and saturates at the longest step.
   Applies to 5xx and to status-less transport failures. Overridable with
   `TUSKER_RETRY_TRANSIENT_ESCALATION_SECS` (comma list); malformed values
   fall back to the defaults rather than disabling escalation. A quota
   hint on a 5xx body still wins and earns the quota window.

2. **Type-agnostic cooldown classification** (`cooldown.py`,
   `endpoints.py`). `_cooldown_seconds_for_provider_error` now classifies by
   exception attributes (`upstream_status`, `code`, `upstream_body`,
   `retry_after_secs`) rather than by class, and `_cooldown_for_exc` no
   longer returns `None` for non-`GatewayError` exceptions. All four
   `breaker.record_failure(..., cooldown_secs=...)` call sites dropped the
   `isinstance(exc, GatewayError)` gate, so a provider failure surfacing as
   a raw exception still earns the cooldown its status implies.

3. **Aggregate attempt budget** (`endpoints.py`). New
   `TUSKER_PROVIDER_TOTAL_ATTEMPT_BUDGET_SECS` caps the *sum* of candidate
   attempts, measured from the first dispatch. When the budget is spent the
   loop stops dispatching and raises `ProviderError(code="aggregate_attempt_budget")`
   instead of starting another doomed attempt. Unset means no cap
   (backwards compatible); `_provider_attempt_timeout_secs` also clamps the
   per-attempt budget to the remaining aggregate budget.

`CircuitBreaker.consecutive_failures(provider, model)` was added so the
caller can read the count *before* recording the new failure; using the
post-increment count would start the ladder one step early.

## Still open

4. **Widen the permanent mark.** 405/409/413/422 currently earn the 3600s
   cooldown but are not added to the permanently-failed set, so the
   auto-free pool re-ranks them after the window. Worth a deliberate
   decision either way rather than the current implicit split.

5. **Aggregate budget default.** Production leaves
   `TUSKER_PROVIDER_TOTAL_ATTEMPT_BUDGET_SECS` unset, so the cap is inert
   until enabled deliberately. A sensible production value is some way
   under `TUSKER_REQUEST_TIMEOUT_MS=240000`.
