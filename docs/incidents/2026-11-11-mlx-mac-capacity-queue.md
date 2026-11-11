# MLX Mac capacity rejections: local admission discarded servable work (2026-11-11)

## Symptom

`req_670cb08d95d0f14e` (2026-11-11T04:48Z, `hermes-privacy`, stream) reached the
client as:

> Upstream provider capacity is unavailable; retry shortly. Request ID: req_670cb08d95d0f14e.

The request was an OMP turn with 13 tools and a required image input, prompt
estimate ~65K-188K tokens depending on the turn. It consumed 11 of its 12
candidate attempts and returned HTTP 200 with an SSE error frame.

## What actually happened

The gateway was healthy; the privacy pool's fallback chain ran out. Per-candidate
outcomes for that request:

| Candidate | Outcome |
|---|---|
| `openai-codex/gpt-5.6-luna` | `503 no usable OAuth credential` (all seats cooling) |
| `github-copilot-enterprise/*` | `quota exceeded` (provider-wide cooldown) |
| `gpt-4.1` / `gpt-4o` | `400` prompt 65,470 > 64,000 token limit |
| `mlx-mac/qwen3.8-27b` | `503 provider capacity unavailable` |

Only the last one was avoidable. The provider usage ledger for the day shows the
scale:

| Provider/model | Requests | Successes | Failures | Capacity rejections |
|---|---|---|---|---|
| `mlx-mac/qwen3.8-27b` | 80 | 3 | 77 | 77 |
| `mlx-mac/shirdel-coder-9b-claude-fable-5` | 80 | 5 | 75 | 67 |
| `apim/gpt-5.6-luna` | 1122 | 1063 | 59 | 0 |

## Root cause

`CapacityController.acquire()` was fail-fast: once `TUSKER_MLX_MAC_MAX_CONCURRENT`
(2) in-flight requests were admitted, the next request was rejected *locally*
without ever contacting the Mac, then the group was quarantined for
`TUSKER_PROVIDER_CAPACITY_BUSY_COOLDOWN_SECS` (15s) so every sibling MLX route
was skipped too.

That policy assumed a saturated local backend cannot serve the request. Direct
probes disproved it for this backend: `mlx-mac/qwen3.8-27b` answered in 7.1s
cold and 1.7s warm against the live host, and a gateway-routed `hermes-privacy`
request (text+tools and image+tools) returned HTTP 200 in 1.6-2.0s. The Mac was
busy, not broken — the gateway was discarding work the backend would have
served.

## Fix

Bounded FIFO admission in `tusker_gateway/provider_usage.py`:

- `TUSKER_<GROUP>_QUEUE_DEPTH` (default `0`) holds up to N waiters in arrival
  order; `TUSKER_<GROUP>_QUEUE_WAIT_SECS` (default `60`) bounds the wait.
- Claim-or-enqueue happens in one critical section, so a fresh arrival cannot
  jump waiters already queued.
- `_active` is only incremented by a successful claim and only decremented by
  `release`, so the queue cannot double-release a slot on timeout or
  cancellation (a handoff-future design would have needed that guard on every
  exit path).
- Queueing is opt-in, so every other group keeps fail-fast behaviour.

`PassthroughClient._reserve_capacity()` became `async` and calls
`acquire_queued()`; both dispatch sites await it. `snapshot()` now reports
`queued`, `queue_depth`, and `queue_wait_timeouts` alongside `active`/`limit`/
`rejections`.

Production config (`k8s/deployment.yaml`): `TUSKER_MLX_MAC_QUEUE_DEPTH=3`,
`TUSKER_MLX_MAC_QUEUE_WAIT_SECS=120`, `mlx-mac` attempt budget raised from 240s
to 420s, `TUSKER_REQUEST_TIMEOUT_MS=420000` and
`TUSKER_MAX_REQUEST_TIMEOUT_MS=600000`. The attempt budget had to grow with the
queue: a queued request spends its slot wait before the first upstream byte and
the idle clock does not reset until then. `TUSKER_MAX_REQUEST_TIMEOUT_MS` also
had to be raised because the deadline loader clamps the default down to it —
leaving the 300s built-in would have silently restored a 300s cap.
`TUSKER_PROVIDER_STREAM_TIMEOUT_OVERRIDES_JSON` gained `"mlx-mac": 420` so the
socket idle window matches the attempt budget.

`TUSKER_LOCAL_LLM_QUEUE_DEPTH` stays at `0`: the Jetson is an 8 GiB device with
short requests, where queueing would only add latency.

## Tests

`tests/test_provider_usage.py`:

- `test_queue_disabled_group_still_rejects_immediately` — default groups keep
  fail-fast, and the rejection is counted.
- `test_queued_waiter_takes_the_slot_when_it_frees` — a waiter acquires the
  slot on release instead of a 503.
- `test_queue_rejects_beyond_configured_depth` — a full queue fails over
  immediately rather than growing without bound.
- `test_queue_wait_expiry_returns_rejection` — the wait budget is enforced and
  counted as `queue_wait_timeouts`.
- `test_queued_reserve_holds_slot_for_the_request` — the queued path hands the
  caller a real counted lease, not a free pass.
- `test_queue_honours_arrival_order` — three waiters are served oldest-first.
- `test_local_capacity_error_is_client_safe` and
  `test_process_backed_capacity_gate_rejects_fourth_concurrent_request` now
  await the async reserve.

All seven new tests fail against the pre-fix source. Full offline suite:
`2136 passed, 8 skipped` (`--ignore=tests/test_passthrough_providers.py`).

## Residual risk

- Queueing cannot help when the backend genuinely cannot serve the request. A
  cold-loading Mac that takes a queued request stalls the whole queue; the wait
  bound (120s) is what keeps that from becoming a caller-visible hang.
- The composite image-plus-tools gate still excludes `mlx-mac/qwen3.8-27b`
  (probe verdict `no_tool_call`, see
  `2026-11-01-composite-probe-starvation.md`), so image+tools turns select it
  only through the bounded recovery pass.
- `github-copilot-enterprise` quota exhaustion is an account condition the
  gateway cannot route around; it only stops the pool from spending attempts on
  routes that will 429.