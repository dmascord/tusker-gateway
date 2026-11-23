## 2026-11-22 — Privacy probe starvation and context-window evidence check

Follow-up to `2026-11-22-failover-terminal-error.md`. That incident showed the
privacy pool had 197 configured routes but zero eligible candidates for an
image+tools ~212K-token turn. This record explains why the 70-route reserve of
auto-discovered Command Code models never became usable, and what the context
window evidence actually says about the three routes dropped at 212K.

## 1. Privacy probe starvation (fixed)

### Evidence

The live capability DB (`privacy_zdr`, 205 records) broke down as:

| status | count | failure classes |
|---|---:|---|
| `passed` | 43 | mostly `openai-codex`, `opencode-go`, `synthetic` from 2026-11-20 03:46 |
| `unavailable` | 162 | `rate_limit_exceeded` 106, `provider_error` 30, `forbidden` 21, `timeout` 4, `oauth_credential_unavailable` 1 |

105 of the `unavailable` rows were written in one burst on 2026-11-20 03:4x
with `http_status = NULL` — upstream 429s that overwrote whatever the probe
would otherwise have learned.

The periodic job ran every cycle with `limit=8`. Querying rows newer than 4h
showed the **same eight routes every time**: `commandcode/Qwen/Qwen3.8-Max-Prime`,
`Qwen4-Max`, `claude-fable-5`, `claude-fable-5-1`, `Qwen/Qwen3.8-Omni-Flash`,
`Qwen/Qwen3.8-Flash`, `Qwen/Qwen3.8-Max`, `Qwen/Qwen3.8-Max-0902` — all
`unavailable`. Roughly 180 other auto-discovered privacy routes were never
examined.

### Root cause

`run_privacy_qualification` selected candidates with a plain `sorted(pairs)[:limit]`
and `_needs_probe` treated a fresh `unavailable` record as *not due*. The eight
alphabetically-first `commandcode/Qwen/*` routes were therefore the first eight
every cycle, always returned `unavailable`, and were never replaced. This is the
same defect already fixed for composite qualification on 2026-11-01 (see
`2026-11-01-composite-probe-starvation.md`), which introduced
`_interleave_probe_candidates` precisely so one quota-blocked provider cannot
consume a bounded batch.

### Fix

1. `tusker_gateway/tool_qualification.py`: `_interleave_probe_candidates` takes
   an optional `capability` argument (default `COMPOSITE_CAPABILITY`) so the
   same ordering guard can be reused for `privacy_zdr`.
2. `tusker_gateway/privacy_qualification.py`: treat `unavailable` as due, then
   interleave the due set across providers before applying `limit` — also for
   explicit `--force` runs. Removed the now-unused `_needs_probe`.
3. `tests/test_privacy_qualification_ordering.py`: regression proving a
   bounded batch reaches two providers when all records are freshly
   `unavailable` (would have probed zero under the old logic).

Note: the Command Code routes are not merely a probe artifact — their evidence
includes 21 `forbidden` (403) and 30 `provider_error` (400/422/404) verdicts,
so many genuinely refuse the ZDR probe. The fix ensures the remaining routes
get their chance instead of being hidden behind the noisy prefix.

## 1b. Composite image+tools re-run (one-off, limit=32)

Re-ran `run_image_tool_qualification` against the `privacy` pool with `limit=32`,
`max_concurrency=2`, `timeout_secs=25` (default configured batch is 16):

```
tested=32, passed=1
rate_limited=16, provider_error=7, service_unavailable=4,
non_strict_tool_contract=2, timeout=2, other=1
passed: mlx-mac/ornith-1.5:35b
```

The single new pass does **not** immediately add a usable privacy candidate:
that route's `privacy_zdr` evidence is still `unavailable` (timeout), so the
privacy-evidence gate keeps it out. With half the batch rate-limited, shortening
the periodic interval or permanently doubling the batch would add pressure to
already quota-bound providers rather than yield more passes, so neither the
configured `TUSKER_COMPOSITE_QUALIFICATION_INTERVAL_SECS` nor `..._LIMIT` was
changed. Re-run after the relevant provider quota windows reset.

## 2. Context-window verification (no declaration changes)

Fetched live `/v1/models` metadata from the two providers and compared with the
declared windows that filtered the 212,731-token request:

| Route | Declared | Upstream published `context_length` | Verdict |
|---|---:|---:|---|
| `synthetic/hf:openai/gpt-oss-120b` | 131072 | 131072 | correct — genuinely too small for 212K |
| `synthetic/hf:zai-org/GLM-4.7-Flash` | 196608 | 196608 | correct — genuinely too small for 212K |
| `apim/gpt-5.6-luna` | 128000 | *none published* | **wrong** — 128000 is the max OUTPUT, not the total window |

APIM's raw catalog row for `gpt-5.6-luna` carries only `id`, `object`,
`capabilities` and lifecycle fields — no context field, so the gateway fell
back to its built-in `apim = 128000`. Operator evidence (2026-11-22) establishes
that this deployment of `gpt-5.6-luna` on Azure OpenAI / Microsoft Foundry via
APIM has a **1,050,000-token total context window** and a **128,000-token
maximum output limit**. That is corroborated by the `opencode-go` catalog, which
publishes 1,050,000 for the same model slug. No learned windows exist for any
of the three routes, and the observed `400 {"error":"Context limit exceeded"}`
from `synthetic/gpt-oss-120b` carries no number to learn from.

### Fix

`k8s/deployment.yaml` — added the model-scoped override
`"apim/gpt-5.6-luna": 1050000` to `TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON`.
Scoped to the model rather than the provider because other APIM models may
differ. The 128,000 output cap is enforced upstream; `fit_output_budget` only
clamps `max_tokens` down to the remaining *total* window, so a normal 16,384
request is unaffected and no separate output-limit config is needed.

Regression test in `tests/test_context_limits.py` pins the 1,050,000 window and
asserts a 16,384-token cap is not clamped for a 208K prompt (the original
filtered request). Because this setting is an env var read per call, the change
takes effect on pod restart, not via the DB config store.

### Verification (deployed)

- Source commit `e1c0a2bdd6ab161a898612c72012010bd9d5e810`, image digest
  `sha256:4db8c554b74dd4c64d6c988d22d688a4b1c9b0f9a0b4bf56c41195b12b322662`;
  `k8s/verify-provenance.sh` reports tracked manifest, live Deployment, running
  pod imageID and `/health` commit all agree.
- In-pod check: override present, `declared_context_window("apim","gpt-5.6-luna")
  == 1050000`, and `fit_output_budget` leaves a 16,384 cap unclamped for a
  208,379-token prompt.
- In-pod check: deployed `privacy_qualification.run_privacy_qualification` uses
  the interleave guard and the dead `_needs_probe` is gone.
- Deploy smoke: `/health` 200, `/ready` 200, `{"ok": true, "reason": "ok"}`.
- Full offline suite: 2205 passed, 8 skipped.
