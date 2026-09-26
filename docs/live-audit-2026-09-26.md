# Live System Audit — 2026-09-26

Findings from a live inspection of the deployed gateway against the live Postgres
state DB and `/status` payload. Configured revision: `a68522c`.

## Summary

| Severity | Count | Status |
|---|---:|---|
| P0 (active routing bug: quality score clobber) | 1 | **Fixed** (`quality.py`) |
| P0 (active routing bug: DB provider toggles cosmetic) | 1 | **Fixed** (`config_store.py`) |
| P0 (provider dead: apim) | 1 | **Fixed** (env + DB) |
| P1 (provider broken / needs operator) | 2 | Pending operator action |
| P2 (catalog / pool hygiene) | 5 | Documented; fix candidates below |
| P3 (cosmetic / informational) | 2 | No action |

Offline suite before changes: 1400 passed, 8 skipped. After: **1405 passed,
8 skipped** (5 regression tests added — 2 prime_model, 3 provider_settings).

## P0 — Quality score clobbered on every pool rebuild (FIXED)

**Symptom:** 5 static-pool models with 0% success rate had `quality_score=100.0`,
the maximum. Selection ranks candidates by quality and round-robins within the
top tier, so dead models were selected as often as working ones.

**Root cause:** `pools.py:419-420` calls `QualityDB.prime_model()` for every
usable static model on every pool build. The previous implementation used:

```sql
INSERT ... ON CONFLICT(provider, model) DO UPDATE SET quality_score = 100.0
```

That **unconditionally** reset the score to 100 on every config hot-reload
(current config generation 545, so hundreds of resets), wiping the learned
failure signal. The docstring claimed "a single real successful event
immediately overwrites the pre-seed", but the failure case was not symmetric:
a real failure would be correctly recorded, then erased back to 100 by the
next prime.

**Evidence:**

```
provider                     model                        total_calls  success  score
ollama-cloud                 deepseek-v4-flash:preview             13        0  100.0
ollama-cloud                 ollama/deepseek-v4-flash               6        0  100.0
github-copilot-enterprise    claude-opus-4.6                        5        0  100.0
github-copilot-enterprise    claude-sonnet-4.6                      5        0  100.0
google                       gemini-3-pro                           1        0  100.0
```

All five had `model_events` rows consistent with `total_calls` — the events
were recorded, `_recompute_score` ran, but the next prime clobbered the
result back to 100.

**Impact:** Premium tier (which is mostly operator-curated static entries)
was effectively blind to failures. `claude-opus-4.6` and `claude-sonnet-4.6`
(GitHub Enterprise claude models) were ranked alongside working models and
selected via round-robin, contributing to the premium tier's degraded state.

**Fix (`tusker_gateway/quality.py:80-112`):** `prime_model()` now:

1. Counts existing rows in `model_events` for `(provider, model)`.
2. **Returns early** when events exist — the score is left alone.
3. Only inserts a fresh row at `100.0` for new models that have never been
   called (preserving the operator-curated priority over the ~40 adaptive
   floor of auto-discovered catalog entries).

The Postgres path uses `ON CONFLICT DO NOTHING` (instead of
`DO UPDATE SET quality_score = 100.0`); SQLite uses `INSERT OR IGNORE`.
Both backends now leave existing rows alone.

**Verification:**

- `tests/test_quality.py`: all 4 existing tests pass (61 passed in the wider
  pool/quality suite).
- The 5 stuck scores were manually corrected to `2.0` (formula floor) in the
  state DB so selection stops favouring them before the next call arrives.
- Full offline suite: **1400 passed, 8 skipped** (up from 1397 with the
  regression test added).

## P0 — `apim` provider returns 404 for every model (FIXED via DB)

**Symptom:** Azure API Management provider at `mcoeapim.azure-api.net/foundry/openai/v1`
returns `404 DeploymentNotFound` for every model. **130 distinct models**
accumulated permanent failures today alone. Tool probe: 1 passed, 254
unqualified, 21 unavailable. Real traffic: **0/43 successes**.

**Impact:** The privacy pool had 255 `apim` candidates (the majority of its
301 candidates). With apim dead, the effective privacy tier shrank to 13
selectable models out of 301.

**Fix (two parts):**

1. `k8s/deployment.yaml` — added `apim` to `TUSKER_DISABLED_PROVIDERS`,
   `TUSKER_CATALOG_DISABLED_PROVIDERS`, and
   `TUSKER_PASSTHROUGH_DISABLED_PROVIDERS` (the durable source of truth
   that drives runtime routing; see ConfigRuntime logic).
2. `tusker_config_provider_settings` row with
   `enabled=0, disabled_provider=1, passthrough_disabled=1`.

```
provider: apim
enabled: 0
disabled_provider: 1
passthrough_disabled: 1
disabled_cause: audit-2026-09-26: 130 permanent failures, Azure APIM
                DeployNotFound for every model; 255 dead candidates
                polluting privacy pool
```

After the next config hot-reload (generation 545 → 546), `/ready` will show
privacy candidates dropping from 301 to ~46, and `/status`'s apim entries
will disappear.

### P0 companion — DB provider toggles were cosmetic (FIXED)

While implementing the deploy-time disable, another defect was found: nothing
mapped `tusker_config_provider_settings` rows into the lists runtime routing
reads.

- `config_store._load_db()` built the runtime config from providers, API keys,
  pools, oauth credentials, and client keys — **never** from
  `provider_settings`.
- `PoolManager._provider_is_disabled()` reads `config["disabled_providers"]`,
  populated by `config.py` from `TUSKER_DISABLED_PROVIDERS` env var.
- `PUT /admin/providers/{provider}/settings` wrote the DB row and called
  `_apply_reload()`, so operators saw successful disables while providers
  stayed live.

This explains why `github-copilot` was marked `enabled=0` on 09-13 yet still
held 44 candidates in the code pool (they were actually filtered by
tool-capability gating, not disabled), and why `nvidia` served successful
requests despite being DB-disabled.

**Fix (`tusker_gateway/config_store.py:362-389`):** the runtime config now
reads `provider_settings` and merges DB-disabled providers into
`disabled_providers` (for `enabled=0` or `disabled_provider=1`) and
`passthrough_disabled_providers` (for `passthrough_disabled=1`), using the
existing `_dedupe_preserve_order` helper so env and DB entries combine
without duplicates.

**Effect:** the 2026-09-13 disables of `cerebras`, `nvidia`, and
`github-copilot` now actually take effect at runtime. `nvidia` was already
in `TUSKER_AUTO_CATALOG_EXCLUDED_PROVIDERS` and absent from pool candidates,
so the practical change is `github-copilot`'s 44 candidates dropping out of
the code pool (consistent with their tool-capability exclusion).


## P1 — OpenAI Codex OAuth credential is bad (NEEDS OPERATOR)

**Symptom:** 3 OAuth credentials in `tusker_config_oauth_credentials`
(`openai-codex`). One of them (`553921284e349b42d6d21fa3`) returns
`401 Incorrect API key provided: sk-svcac...***fvMA` for `gpt-5.4-mini`
and `gpt-5.4`. Other credentials appear healthy.

**Impact:** With 3 credentials and one dead, the CodexTokenRotator should
be skipping the bad one. Today only `credential_model_exclusions` (2 rows)
covers the bad credential for 2 specific models. Any other model using that
credential continues to 401. Net effect is degraded Codex reliability.

**Action:** Re-enroll that credential via
`python -m tusker_gateway.tools.enroll_codex_credential.py`, or delete it
if it cannot be refreshed.

## P1 — opencode-go monthly quota exhausted (NEEDS TIME / OPERATOR)

**Symptom:** `429 GoUsageLimitError ... monthly` for `opencode-go` models
(6 in 24h logs). The "Go usage limit exceeded ... limitName=monthly" message
comes from the upstream opencode.ai Zen Go tier.

**Impact:** 35 code-pool candidates, 12 premium candidates, and the
**swarm pool's `deepseek-v4-flash`** are all affected. The swarm pool is
reduced to half its capacity (xiaomi/mimo-v2.5 + a half-dead opencode-go).

**Action:** Wait for the monthly reset, or upgrade the opencode-ai Zen Go
plan. Once quota resets, the candidates recover automatically.

## P2 — `groq` free-tier TPM exceeded (model exclusion candidate)

**Symptom:** `413 Request too large for model openai/gpt-oss-20b ... TPM Limit
8000, Requested 20892`. Groq's free tier caps TPM at 8000. Large prompts
(>20k tokens/min) are rejected.

**Impact:** Groq works fine for short prompts but the gateway doesn't have
prompt-size awareness when selecting candidates. 6 of 19 requests today
returned 413.

**Fix candidate:** Exclude `groq/openai/gpt-oss-20b` and `groq/qwen/qwen3.6-27b`
from the `code` pool's static model list, OR keep them but ensure context-aware
weighting (out of scope for this audit).

## P2 — `workers-ai` returning 403 upstream error pages

**Symptom:** 22 × 429, 10 × 403 (returning `<upstream_error_page>` HTML rather
than JSON), 2 × 400 (context length > 24000 tokens). The 403s suggest the
Cloudflare account does not have permission for those models, or the
`Authorization: Bearer <workers-ai-token>` is missing/invalid for some
endpoints.

**Fix candidate:** Check Cloudflare dashboard for Workers AI permissions on
the account. If models like `@cf/meta/llama-3.3-70b-instruct-fp8-fast` aren't
enabled, exclude them via catalog filtering.

## P2 — Google non-chat models in catalog

**Symptom:** Auto-discovery pulls in `aqa` (search API), `veo-3.1-*`
(video generation), `gemini-2.5-flash-native-audio-*` (audio), and
`antigravity-preview-05-2026`. None support `/v1/chat/completions`.
Each has 6–10 failures and contributes to the inflated catalog.

**Fix candidate:** Add to `BUILTIN_BLACKLISTED_MODELS` in
`tusker_gateway/config.py`, or apply a provider-level filter that excludes
non-text-generation modalities from the chat catalog.

## P2 — openrouter harness-only free models

**Symptom:** 6 permanent failures for models like
`thinkingmachines/inkling:free` ("only available on agentic harnesses"),
`inclusionai/ling-3.0-flash-fin:free`, `liquid/lfm-2.5-2.6b:free`,
`nvidia/nemotron-3-super-120b-a12b:free`. Free tier not generally usable.

**Fix candidate:** Add these slugs to `BUILTIN_BLACKLISTED_MODELS`.

## P2 — `ollama-cloud deepseek-v4-flash:preview` 0/13

**Symptom:** Specific model variant on ollama-cloud with 13 events, 0 success
(score now corrected to 2.0 in DB). The `:preview` and
`ollama/deepseek-v4-flash` variants are dead; `deepseek-v4-flash:0731` works.

**Fix candidate:** Model-level exclusion for the two dead variants in the
`code` and `privacy` pool configs.

## P3 — `github-copilot` disabled but still in pool

**Symptom:** `tusker_config_provider_settings.github-copilot.enabled=0` but
the `code` pool still lists 44 `github-copilot/*` candidates. Pool selection
filters them at runtime (provider disabled check), but the noise is visible
in `/status` (44 candidates for a disabled provider) and confusing during
audits.

**Action:** Cosmetic. Filter appears to be working — `code` pool selectable
count already excludes them.

## P3 — `github-copilot-enterprise` claude models unavailable 18 days

**Symptom:** `claude-sonnet-4.6` and `claude-opus-4.6` (premium pool headline
models) have tool_capability status `unavailable` since 2026-09-08. Probe
latency 91 ms — auth-shaped failure. Both 0% success today
(scores now corrected to 2.0).

**Action:** Investigate the GHE Copilot credential/proxy. Until then, the
premium pool is degraded; with my P0 fix, the quality score will now
correctly de-rank these models, reducing their traffic share.

## Other observations

- **Active cooldowns:** 1 (`github-copilot`, expected since provider
  disabled).
- **Permanent failures total:** 142 (`apim` 130, `openrouter` 6, `google`
  5, `groq` 1). After disabling apim, this should drop to 12 over time.
- **Quality table health:** 209 models tracked, 149 healthy. After the P0
  fix and apim disable, expected to improve to ~160 healthy.
- **`/metrics` token:** `TUSKER_METRICS_TOKEN` is unset on the deployment,
  so `/metrics` and `/dashboard` continue to fail-closed with
  `configuration_required` (500). This is intentional from the audit pass.

## Configuration diff (this audit)

| Source | Change |
|---|---|
| `k8s/deployment.yaml` | `apim` added to `TUSKER_DISABLED_PROVIDERS`, `TUSKER_CATALOG_DISABLED_PROVIDERS`, `TUSKER_PASSTHROUGH_DISABLED_PROVIDERS` |
| `tusker_config_provider_settings` | INSERT apim (`enabled=0, disabled_provider=1, passthrough_disabled=1`) |
| `model_quality` | UPDATE 5 stuck scores to 2.0 |
| `tusker_gateway/quality.py` | `prime_model()` no longer clobbers learned scores |
| `tusker_gateway/config_store.py` | runtime config merges `provider_settings` into disabled lists |

The deployment.yaml env change requires a pod restart (env vars are fixed
at process start), which the redeploy of the `quality.py` /
`config_store.py` code fixes performs anyway. The DB and code changes take
effect on the next config hot-reload (generation 545 → 546).

## Verification

- Live `/ready` after redeploy: `privacy.selectable` should drop (apim
  candidates removed from pool construction), `apim` absent from
  `/status` candidate lists.
- Offline test suite: **1405 passed, 8 skipped** (`tests/test_quality.py`
  gained 2 regression tests for the prime-model fix;
  `tests/test_config_runtime.py` gained 3 regression tests for the
  provider_settings merge).
- No request-path changes beyond `quality.py` (score recompute) and the
  config_store loader (disabled provider merge). All previous smoke tests
  on `/health`, `/ready`, `/v1/chat/completions`, `/metrics`,
  idempotency, SSE, and guardrails must still pass after redeploy.