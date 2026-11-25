## 2026-11-25 — Direct provider routes bypassed the cooldown tracker

### Symptom

The per-provider failure ledger showed providers the gateway already knew
were dead still being attempted, with the failures accumulating as new
provider failures against the same route.

Per-provider failure counts for 2026-11-24 (from `provider_usage_daily`):

| Provider | Attempts | Failures | Success rate |
|---|---:|---:|---:|
| kilo | 223 | 124 | 44.4% |
| github-copilot-enterprise | 52 | 52 | 0% |
| google | 51 | 29 | 43.1% |
| commandcode | 27 | 27 | 0% |
| alibaba | 18 | 18 | 0% |
| openrouter | 17 | 15 | 11.8% |
| groq | 10 | 9 | 10.0% |

Every one of those providers carried an active provider-wide cooldown.

### Root cause

Two independent paths dispatched upstream without consulting the cooldown
tracker.

**1. The direct/passthrough route never read cooldowns.**

A request whose `model` names a provider (`provider/model` or
`provider::model`) resolves to a passthrough route
(`routing.py:143`, `routing.py:117`) and skips pool selection entirely.
Pool selection is where the cooldown filter lives
(`pools.py:1995-2004`, `self._cooldowns.is_cooldown(provider, model)`).

On the direct branch in `_call_with_pool_fallback`
(`tusker_gateway/endpoints.py`), the only pre-attempt gate was the circuit
breaker. Cooldowns were *written* on failure (`passthrough.py:2705`,
`passthrough.py:2808`) but never *read* before the attempt.

Confirmed live against z.ai, which was cooled until 2026-11-25 20:30Z
after `{"error":{"code":"1310","message":"Weekly/Monthly Limit Exhausted.
Your limit will reset at 2026-11-25 20:30:50"}}`:

```
GATEWAY_STATUS 429 {"error":{"code":"rate_limit_exceeded",
  "message":"Upstream provider rate limit exceeded; retry in about 73238s."}}
```

The request reached upstream and was recorded as a failure
(`zai/glm-5.3-flash`: 1 request, 0 successes) instead of being refused
locally from the cooldown the gateway was already enforcing.

**2. The composite image+tools qualification runner had no quarantine filter.**

`run_image_tool_qualification` (`tool_qualification.py`) selected probe
candidates from the pool catalog with no cooldown check, while the tool,
modality, and structured runners all skip quarantined routes
(`_route_is_quarantined`). Because the composite probe reaches the gateway
through its public boundary (`http://127.0.0.1:8642/v1/chat/completions`)
with `"model": f"{provider}/{model}"`, it used the direct route above and
re-probed cooled routes on every cycle.

This is the source of the identical-second failure batches in the ledger —
one attempt per model, all with the same verdict:

```
provider          | model                     | ts                     | verdict
github-copilot-enterprise | claude-haiku-4.5 | 2026-11-24 19:54:40+00 | {"non_stream":true,"status":429}
github-copilot-enterprise | claude-opus-5     | 2026-11-24 19:54:40+00 | {"non_stream":true,"status":429}
... (11 models, one attempt each, pool IS NULL)
```

These are probe sweeps, not client turns: 241 of the day's captured
failures carried `pool IS NULL` (the direct route), against 300 on the
`code` pool and 72 on `privacy`.

The pool path was already correct — joining every recorded attempt
against the cooldown tables returns zero attempts inside an active
cooldown:

| Check | Violations |
|---|---:|
| Attempts inside an active `(provider, model)` cooldown (`model_events`) | 0 |
| Attempts inside an active `(provider, model)` cooldown (`turn_captures`) | 0 |
| Attempts inside an active provider-wide cooldown | 0 |

### Fix

- `CooldownTracker.remaining_cooldown_seconds()` (`cooldown.py`): returns the
  longest applicable window for a route — model-scoped, provider-wide,
  shared-capacity group, or global circuit.
- `cooldown_gate_error()` (`cooldown.py`): returns a `RateLimitError`
  carrying the enforced window when a route is still in cooldown, else
  `None`. Keeping it next to the tracker it reads keeps the oversized
  `endpoints.py` from growing further.
- Direct-route gate (`endpoints.py`, `_call_with_pool_fallback`): when the
  route is in cooldown, fail locally with the enforced window in
  `Retry-After` instead of dispatching upstream. The error carries the
  provider so the shared 429 surfaces treat it like the upstream rate
  limit it replaces, which means Enterprise Copilot still answers 503
  rather than exposing a provider quota as a client 429.
- Loopback qualification probes are exempt
  (`_is_loopback_qualification_probe`), because re-probing a quarantined
  route is what the runners exist to decide. The exemption requires a
  loopback peer address, so an authenticated external client cannot claim
  it with the header.
- The composite qualification runner now skips quarantined routes, matching
  the tool, modality, and structured runners.
- Provider-wide 429 responses now honour an explicit `Retry-After` instead
  of always answering a fixed 5s. The fixed value told clients to retry
  into a route the gateway was still quarantining — the same drift the
  `_retry_after_seconds_for_exc` docstring warns about for the JSON
  envelope.

### Verification

`tests/test_direct_route_cooldown.py` (10 tests) and
`tests/test_tool_capability.py::test_composite_runner_skips_quarantined_routes`.

Both are discriminating: with the gate removed the direct-route tests
return 502 (upstream was called) instead of 429, and with the composite
filter removed the cooled route is probed again.

```
$ pytest tests/test_direct_route_cooldown.py -q
10 passed

$ pytest tests/ -q --ignore=tests/test_passthrough_providers.py
```

### Note

The ledger records provider *attempts*, not client turns, so a single
client request that fails over appears under several providers. Failed
429/5xx attempts carry no token counts. Both caveats apply to the tables
above.
