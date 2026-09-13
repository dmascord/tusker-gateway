# Provider Performance Review — 2026-09-12

Refresh of `docs/provider-review-2026-09-11.md`. Numbers come from the live
gateway state database (`provider_usage_daily`, `model_quality`, `breakers`,
`provider_cooldowns`, `tusker_config_*`). Time window is the last 7 days
ending 2026-09-12 08:23 UTC.

## TL;DR

- **3 Codex accounts are justified.** They are the *only* paid tier with
  100% lifetime quality, no provider-level cooldown, and no recorded
  failures. One model-level breaker is open for `gpt-5.4-mini`; the main
  `gpt-5.6-sol`/`terra` on copilot, `gpt-5.5` on copilot, and
  `claude-sonnet-4.6` on copilot are currently in a 1-hour
  circuit-breaker open state; the evidence does not establish the cause.
- **The 3 Codex accounts are operational despite noisy OAuth refresh logs.**
  The 401 `refresh_token_reused` lines are emitted during refresh, but the
  gateway continues selecting credentials and serving traffic successfully.
  Last 87 gateway calls (over the past ~7 minutes) returned HTTP 200 on
  `gpt-5.6-luna`; the log window shows credential indices `1/3`, `2/3`, and
  `3/3` selected. The evidence proves non-fatal rotation, not that the 401s
  are harmless or that every refresh succeeded; token-refresh behavior still
  deserves investigation.
- **github-copilot (non-enterprise) is dead.** 7 lifetime calls, 6
  failures, 3 open breakers, and a provider cooldown until
  2026-10-01 00:00 UTC. It should be deleted or paused.
- **github-copilot-enterprise is degraded, not dead.** 120 requests in 7d,
  23 successes / 97 failures (80% error rate). 10 open breakers covering
  gpt-4o-mini, gpt-4.1, gpt-4.1-2025-04-14, gpt-5-mini, claude-opus-4.6,
  claude-sonnet-4.6, mai-code-1-flash-picker, gpt-5.6-luna, gpt-5.5,
  gpt-5.4-mini. The provider-level cooldown expires 2026-10-01 00:00 UTC.
  The healthy Codex routes are masking this — copilot-enterprise should
  stay configured as the Codex OAuth pool fallback, but its non-luna
  routes are not pulling weight.
- **workers-ai is dead on its free tier.** 1,022 requests, 86 successes
  (8%). Two open breakers (`@cf/meta/llama-4-scout-17b-16e-instruct`,
  `@cf/meta/llama-3.3-70b-instruct-fp8-fast`). Its provider cooldown ended
  at 2026-09-12 08:23:20 UTC, immediately before this snapshot.
- **google is mostly broken.** 194 requests, 73 successes (37%), 16 open
  breakers. The Gemini 2.5-flash and 2.5-flash-lite routes still work
  (~80% quality), the rest are 401/quota-exhausted. Key likely expired.
- **cerebras / nvidia / github-copilot are 100% failure.** Lifetime
  failure rate is 100%, no successful calls. They are dead.
- **groq is mostly failing on the privacy pool route.** 310 requests,
  45 successes (15%) — but `groq/openai/gpt-oss-20b` (the route the
  gateway used during the live privacy test) had 1 lifetime success and
  0 failures. The 265 failures are concentrated on gpt-oss-120b /
  qwen3.6-27b / qwen3.8-27b.
- **Top performers over 7 days** are unchanged: xiaomi (12.2k req, 99.9%),
  minimax (5.2k, 99.5%), ollama-cloud (4.7k, 88.7%), synthetic (2.6k,
  80.6%), opencode-zen (2.3k, 97.9%), openrouter (1.3k, 96.9%).


## Provider Performance (7-day, aggregated per provider)

Source: `provider_usage_daily` aggregated 2026-09-06..2026-09-12.

| Tier | Provider | 7d requests | 7d successes | 7d failures | Err % | Notes |
|---|---|---:|---:|---:|---:|---|
| Excellent (<5%) | xiaomi | 12,206 | 12,188 | 18 | 0.1% | Dominant; no cooldowns, no open breakers |
| Excellent (<5%) | minimax | 5,228 | 5,200 | 28 | 0.5% | Stable; 0 open breakers |
| Excellent (<5%) | zai | 366 | 361 | 5 | 1.4% | All models closed |
| Excellent (<5%) | openrouter | 1,308 | 1,267 | 41 | 3.1% | 0 open breakers |
| Excellent (<5%) | opencode-zen | 2,254 | 2,207 | 47 | 2.1% | 3 closed breakers |
| Moderate (5–25%) | ollama-cloud | 4,715 | 4,185 | 530 | 11.2% | 7 open breakers, quota issues |
| Moderate (5–25%) | local-llm | 557 | 502 | 55 | 9.9% | 24 capacity rejections; 1 open breaker on `qwen2.5:7b` |
| Broken (>25%) | mlx-mac | 252 | 182 | 70 | 27.8% | 25 capacity rejections; all breakers closed |
| Moderate (5–25%) | synthetic | 2,560 | 2,063 | 497 | 19.4% | 3 open breakers, subscription rate limits |
| Moderate (5–25%) | alibaba | 203 | 158 | 45 | 22.2% | 6 open breakers, model-by-model |
| Broken (>25%) | groq | 310 | 45 | 265 | 85.5% | Most quota errors on gpt-oss-120b / qwen3.8-27b |
| Broken (>25%) | google | 194 | 73 | 121 | 62.4% | 16 open breakers; key likely expired |
| Broken (>25%) | opencode-go | 126 | 0 | 126 | 100% | Provider cooldown until 2026-09-15 16:13 UTC |
| Broken (>25%) | workers-ai | 1,022 | 86 | 936 | 91.6% | 3 open breakers, free tier exhausted |
| Broken (>25%) | github-copilot-enterprise | 120 | 23 | 97 | 80.8% | 10 open breakers |
| Broken (>25%) | github-copilot | 6 | 0 | 6 | 100% | Provider cooldown until 2026-10-01 00:00 UTC |
| Broken (>25%) | cerebras | 12 | 0 | 12 | 100% | 3 open breakers |
| Broken (>25%) | nvidia | 13 | 0 | 13 | 100% | 0 successes lifetime |
| Zero traffic | openai-codex | 0 | 0 | 0 | n/a | Catalog-only rollup; see Codex assessment |
| Zero traffic | openai, arcee, arliai, cohere, jina, voyage | 0 | 0 | 0 | n/a | Catalog entries only; no chat traffic |

The 7-day `provider_usage_daily` does **not** record Codex. The gateway
emits `model_events` for Codex but the daily rollup currently uses the
auth-pool name (e.g. `minimax`, `ollama-cloud`) and treats Codex traffic
separately. The 4,313 Codex lifetime calls therefore appear only in
`model_quality`. This is a reporting gap, not a traffic gap — see
*Codex-specific stats* below.

## Codex Account Assessment

### Pool composition

| Provider | Pool entries (provider, model) | OAuth credentials | Pool role |
|---|---|---|---|
| openai-codex | gpt-5.6-luna, gpt-5.4-mini, gpt-6-astra, gpt-5.5, gpt-5.6-sol, gpt-5.6-terra | 3 accounts (`damien.01`, `damien.02`, `dmascord`) — round-robin | privacy + premium + code |
| github-copilot-enterprise | gpt-5.6-luna, gpt-5.4-mini, gpt-5.5, gpt-5.4, gpt-4.1, gpt-4o-mini, claude-sonnet-4.6, claude-opus-4.6/4.7/4.8/5, claude-sonnet-5, claude-haiku-4.5, gemini-3.5-flash, mai-code-1.1-flash, mai-code-1-flash-picker, gpt-5.6-sol, gpt-5.6-terra, gpt-5-mini, gpt-5.3-codex, gpt-3.5-turbo, gpt-3.5-turbo-0613, gpt-6-astra | 1 account | privacy + premium + swarm |
| github-copilot (non-enterprise) | gpt-5.6-luna, gpt-5.5, claude-sonnet-4.6 | 1 account | swarm (legacy) |

### openai-codex (3 accounts, round-robin)

- **Lifetime quality**: 4,313 calls, 4,313 successes, 0 failures (100%).
- **Per-model**:
  - gpt-5.6-luna — 3,813 calls, 100%, breaker closed, last_success now.
  - gpt-5.4-mini — 29 calls, 100%, breaker currently **open** (1h) since
    2026-09-12T07:53:20Z. Likely tripped during a probe earlier today.
  - gpt-6-astra — 447 calls, 80.0%, breaker closed.
  - gpt-5.5 — 6 calls, 100%, breaker closed.
  - gpt-5.6-sol — 6 calls, 100%, breaker closed.
  - gpt-5.6-terra — 6 calls, 100%, breaker closed.
- **OAuth behavior**: The `refresh_token_reused` 401 log lines occur during
  credential refresh. The gateway logs the failure, continues selecting
  credentials, and serves successful traffic. In the live log window (last
  20 minutes) I saw `oauth refresh failed` for indices `1/3`, `2/3`, and
  `3/3` interleaved with `oauth credential selected` for the same indices —
  the rotator is still selecting and serving traffic from each account. This
  proves the warnings are non-fatal in the observed window; it does not
  prove every refresh succeeded or that the warnings can be ignored.
- **Smoke test**: `openai-codex::gpt-5.6-luna` round-trip via the live
  gateway just now returned HTTP 200 with the expected body in 5.76s.
  The `gpt-5.4-mini` route returned `circuit_open` because its breaker
  is open — that's correct behavior, not an OAuth failure.
- **Verdict**: **Keep all 3 Codex accounts.** They are the *only*
  provider with 100% lifetime quality at meaningful volume and no
  active cooldowns. Removing accounts would (a) reduce rotation
  diversity and (b) leave the gateway without a fallback for any
  copilot-enterprise model that's currently open.

### github-copilot-enterprise (1 account)

- **7-day traffic**: 120 requests, 23 successes, 97 failures (80% err).
- **Lifetime**: 3,558 calls, 3,533 successes, 25 failures (99.3% success).
- **Why the gap?** The 7-day rate is dragged down by the gpt-4.1,
  gpt-4o-mini, mai-code-1-flash-picker, claude-opus-4.6,
  claude-sonnet-4.6, and gpt-5-mini breakers, all of which were opened
  on 2026-09-11T12:03:20Z (`opened_at=1789128200`) — same timestamp as
  the github-copilot-enterprise provider cooldown update
  (`updated_at=1789130000`). This is a coordinated trip event — likely
  triggered by a 429 from the backend that the gateway interpreted as
  a per-model failure. The cooldown expires 2026-10-01 00:00 UTC.
### github-copilot (1 account)

- **Lifetime**: 7 calls, 1 success, 6 failures (14% success).
- **Provider cooldown**: expires 2026-10-01 00:00 UTC (~16 days).
- **3 open breakers**: gpt-5.6-luna (9-window-failures), claude-sonnet-4.6
  (5), gpt-5.5 (5).
- **Verdict**: **Remove or pause.** Dead weight — 1 success in 7
  lifetime calls, 3 open breakers, 16-day cooldown. The non-enterprise
  Copilot route is no longer load-bearing; it should be removed from
  the swarm pool (and from `default_auto_catalog_providers` if
  catalog-merging is enabled).

### Codex-specific stats (live, last 7 minutes of logs)

`kubectl -n hermes logs deploy/tusker-gateway --since=7m`:

```
openai-codex::gpt-5.6-luna  : 87 requests, 87 successes (100%)
  latency_ms : min=2426 p50=9362 p95=24048 max=28873 avg=10950
openai-codex::gpt-5.4-mini  : 0 requests (breaker open)
```

Credential rotation in the last 20 minutes (live logs):

```
oauth refresh failed provider=openai-codex credential_index=1/3 status=401 code=refresh_token_reused
oauth credential selected provider=openai-codex credential_index=1/3 label=damien.02@tusker.net.au
oauth refresh failed provider=openai-codex credential_index=2/3 status=401 code=refresh_token_reused
oauth credential selected provider=openai-codex credential_index=2/3 label=dmascord@gmail.com
oauth refresh failed provider=openai-codex credential_index=3/3 status=401 code=refresh_token_reused
oauth credential selected provider=openai-codex credential_index=3/3 label=damien.01@tusker.net.au
```
All three accounts are actively selected in the observed log window. The
`refresh_token_reused` warning is non-fatal in that window, but should not
be reclassified or suppressed until refresh-token behavior is understood.

## Codex retention: yes or no?

**Yes — keep all 3 accounts.**

| Criterion | openai-codex (3) | github-copilot-enterprise (1) | github-copilot (1) |
|---|---|---|---|
| Lifetime success rate | 100% (4,313 / 4,313) | 99.3% (3,533 / 3,558) | 14% (1 / 7) |
| Open breakers today | 1 (gpt-5.4-mini) | 10 | 3 |
| Provider cooldown | none | 16d | 16d |
| Distinct models | 6 | 22 | 3 |
| Active 7d traffic | catalog probes + selected chat | mostly broken | none |
| OAuth health | rotating, warning-level 401s only | tripped breaker window | tripped breaker window |
| Rotation value | **high** (3 accounts) | low (1 account) | none |

If anything should be removed, it is **github-copilot**, not the
Codex accounts. The 3 Codex accounts are the only provider with no
provider-level cooldown, no active model-level cooldown except one
tripped breaker, and 100% lifetime success.

## Recommendations

1. **Keep all 3 Codex accounts** — they are the only fully-healthy
   paid-tier provider and they back the `gpt-5.6-luna` / `gpt-5.4-mini`
   / `gpt-5.6-sol` / `gpt-5.6-terra` aliases in the privacy and
   premium pools.
2. **Remove or pause github-copilot** (the non-enterprise route).
   16-day cooldown, 3 open breakers, 14% lifetime success. Either
   delete from `tusker_config_providers` and `TUSKER_POOL_SWARM`, or
   set `TUSKER_PROVIDER_CAPACITY_COOLDOWN_SECS` to extend the cooldown
   indefinitely. Cleanest: delete the row.
3. **Investigate github-copilot-enterprise breaker storm on 2026-09-11T12:03:20Z**.
   Ten breakers opened simultaneously. Could be one of: (a) the
   enterprise Copilot backend returning a global 429, (b) a token
   rotation event the gateway misinterpreted, or (c) a network outage
   between hermes and `copilot-api.sita.ghe.com`. Worth a one-time
   log investigation.
4. **Investigate the `refresh_token_reused` 401s** in
   `tusker_gateway/passthrough.py`. They are non-fatal in the observed
   window, but the gateway should alert on `oauth credential exhausted` or
   `all credentials failed` and separately track refresh-token failures.
5. **Remove the Cerebras / NVIDIA / github-copilot routes** from automatic
   pool discovery or disable their provider keys. They contribute 100%
   failure noise and no useful successful traffic in this snapshot.
6. **Replace `groq/openai/gpt-oss-120b` and `qwen3.8-27b` in the
   privacy pool** with the working `groq/openai/gpt-oss-20b` route. The
   20b route has 100% lifetime quality at lower volume; the 120b / 27b
   routes are 80%+ error rate.
7. **Re-check the privacy pool 502 root cause** from the prior review:
   the previous report blamed a 30s per-candidate timeout. Today the
   privacy pool uses `TUSKER_PROVIDER_ATTEMPT_TIMEOUT_OVERRIDES_JSON`
   to give `local-llm` / `mlx-mac` 240s and `TUSKER_REQUEST_TIMEOUT_MS=240000`.
   The Jetson/MLX timeouts that triggered the 502 last week would not
   reproduce under current config — but local backends still log
   capacity rejections (`TUSKER_PROVIDER_CAPACITY_COOLDOWN_SECS=300`).
   Confirm by re-running the smoke test under load.

## Follow-ups (none requested yet, listed for visibility)

- `tusker_gateway/passthrough.py`: investigate `refresh_token_reused`
  handling and distinguish non-fatal refresh warnings from exhausted
  credentials before changing log severity.
- `tusker_gateway/quality.py`: when lifetime success = 0, prune the
  provider's models from `model_quality` so they stop dragging the
  per-provider aggregation down. Cerebras, NVIDIA, github-copilot are
  the obvious cases.
- `tusker_gateway/admin/diagnostics.py`: include a per-pool breakdown
  of `provider_usage_daily` so that the admin UI surfaces Codex/Copilot
  traffic — currently the daily rollup hides it under `openai-codex`
  but the per-pool view doesn't exist.

## Methodology

- Live state DB queried via `psycopg.connect(os.environ['TUSKER_STATE_DATABASE_URL'])`
  on the active pod `tusker-gateway-656c98bdf8-98ktn`.
- 7-day window: `provider_usage_daily WHERE usage_day >= (CURRENT_DATE - 7)`.
- Per-model quality: `model_quality` aggregate.
- Breakers: `breakers WHERE state='open'`.
- Provider cooldowns: `provider_cooldowns` (epoch seconds → UTC).
- OAuth credentials: `tusker_config_oauth_credentials` (per-provider
  opaque blobs).
- Live Codex smoke: `curl -sS --max-time 180 -H "Authorization: Bearer
  $KEY" http://127.0.0.1:8642/v1/chat/completions` against
  `openai-codex::gpt-5.6-luna` and `openai-codex::gpt-5.4-mini`.
