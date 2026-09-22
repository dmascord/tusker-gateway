# Provider Performance Review — 2026-09-22

Full audit requested 2026-09-22. Sources:

- **7-day usage**: `provider_usage_daily` from the live state DB
  (2026-09-16 .. 2026-09-22), queried inside the gateway pod.
- **Live probes**: 217 requests through the gateway's explicit
  `provider/model` routes (82 sampled models across 18 providers), testing
  text chat, vision (base64 PNG, "what shapes do you see"), tool calling
  (get_weather schema), embeddings, and rerank. Concurrency ≤ 4 with key
  rotation to stay under the per-key rate limiter and below the audit-layer
  failure threshold (see below).
- **Gateway state**: `/admin/pools`, `/admin/providers`, `/admin/catalog`,
  `/admin/breakers`, `/admin/cooldowns`, `/admin/usage`, `/health`
  (generation 306, commit `c49203b`).
- Gateway runtime: running pod `tusker-gateway-857678c69-h46vs`,
  1 replica, postgres state store healthy.

Probe traffic is included in *today's* usage counters but **excluded** from
the 7-day table below (its first day, 2026-09-22, is partially included in
the 7-day window — mlx-mac/local-llm numbers are inflated by the concurrency
tests; treat those two with that caveat).

## TL;DR

- **A new availability bug was found and reproduced: the fail-closed audit
  logger breaks under concurrency.** `AuditLogger._append` does
  open → flock → tail-scan → write → fsync on the NFS-mounted
  `audit.jsonl`; at ≥8 concurrent requests, flock/fsync raises `OSError`
  and — because `TUSKER_AUDIT_FAIL_CLOSED=true` — every such failure is
  returned to the client as **503 audit_unavailable**. Measured on the
  provider-free endpoint `GET /v1/models`: 0% failures at concurrency 1–4,
  **45.8% at concurrency 8, 75.0% at concurrency 16**. This is production
  impact today (23 OSErrors observed in logs during one probe burst).
  **Post-deploy verification (2026-09-22)**: fix deployed (commit
  `a0a4afac`), DB config propagated (generation 306 → 361). Live
  re-test of `GET /v1/models` at concurrency 1/8/16/32 (24 requests
  each): **0 audit 503s at every level** (was 45.8% at c=8, 75.0% at
  c=16); pod logs show zero `audit write failed` and zero retries —
  the in-process lock removes the contention before the retry path is
  needed.
- **APIM is completely dead upstream** (0/162 7-day, 0/102 today).
  Every probe returns HTTP 401 `invalid subscription key` — the
  endpoint is reachable but rejects the gateway's API key.
  Removed `apim/gpt-5.6-luna` from `TUSKER_POOL_PRIVACY` in commit
  `a0a4afa` (2026-09-22); each privacy request no longer pays a
  dead-upstream failover penalty.
- **github-copilot (both), groq, workers-ai, openai-codex, opencode-zen
  are 0% in live probes** and remain heavily breaker-open (113 open
  breakers of 680 tracked). opencode-zen regressed badly vs. its 97.9%
  7-day figure from the Sep-12 review — it is now 41% 7-day error and 0/10
  in probes.
- **Tool qualification IS running**: the live NFS-backed SQLite DB
  (`model_tool_capability.db`) has 191 rows — 102 passed under probe
  version `stream-tool-contract-v1`. The audit initially reported
  `tool_capability: null` because it read from a stale Postgres
  snapshot (`migrate_state.py`) rather than the live runtime store.
  Privacy pool coverage is thin because 407/429 candidates are
  quarantined (dead-provider cooldowns); `require_tool_qualification`
  is effective for the ~22 non-quarantined candidates. A targeted
  qualification run on 2026-09-22 confirmed: `mlx-mac/ornith-1.5:35b`
  passed, `mlx-mac/qwen3.8-27b` failed (no tool call support).
- **voyage + jina are configured for embed/rerank but unusable**: both
  have `embed_path`/`rerank_path` set but **no model catalog**
  (`models_path` unset), so the gateway rejects every model with
  "No healthy upstream model is currently available". `cohere/rerank-v3.5`
  works (0.5 s); `openai/text-embedding-3-*` works (~1.1 s).
- **Healthy core**: minimax (0.5% 7-day err, 15/15 probes), xiaomi (1.0%,
  5/5), zai (1.7%, 11/11), ollama-cloud (4.9%, 12/12 probes), alibaba
  (4.4% but 2/5 probe failures — the failures concentrated on
  `qwen3.8-max`/`qwen3.7-plus`, which are breaker-open), google (23.7% 7d
  — recovered from 62% on Sep-12, but still flaky; 3/5 probes ok),
  opencode-go (18.6% 7d, 10/10 text+tools probes but **no vision** sampled
  — its vision model `deepseek-v4-flash-vision-exp` is breaker-open),
  synthetic (82.7% 7d err; probes: 4/5 text failures are HTML error pages
  on the `hf:` routes, the 5th is a deliberate blacklist rejection of
  `hf:moonshotai/Kimi-K3`; vision 4/5 and tools 4/5 pass),
  mlx-mac (healthy functionally: text 4/4, vision 1/1; tools 1/4 because
  only `ornith-1.5:35b` is tool-qualified — the newly added
  `qwen3.8-27b` has no tool probe yet; the 42.6% 7d error includes my own
  capacity-gate test traffic).
- **openrouter recovered partially** (22.3% 7d vs 96.9% on Sep-12 — the
  397 failures are concentrated in `:free` models; non-free routes work).

## 7-day provider performance (2026-09-16 .. 2026-09-22)

Source: `provider_usage_daily` (state DB), all models.

| Tier | Provider | 7d req | 7d ok | 7d err % | Live probes (t/v/tools) | Notes |
|---|---|---:|---:|---:|---|---|
| Excellent | minimax | 13,838 | 13,771 | 0.5% | 5/5, 5/5, 5/5 | 0 open breakers; dominant |
| Excellent | xiaomi | 3,335 | 3,303 | 1.0% | 2/2, 1/1, 2/2 | mimo-v2.5 solid; tools slow (18.8 s) |
| Excellent | zai | 1,908 | 1,875 | 1.7% | 5/5, 1/1, 5/5 | glm-4.7 + glm-5.3-flash |
| Excellent | ollama-cloud | 2,971 | 2,826 | 4.9% | 5/5, 2/2, 5/5 | kimi-k2.6, glm flash routes |
| Excellent | alibaba | 295 | 282 | 4.4% | 3/5, 3/5, 3/5 | failures on qwen3.7+/3.8-max (breaker-open) |
| Good | opencode-go | 1,322 | 1,076 | 18.6% | 5/5, —, 5/5 | text+tools healthy; vision model breaker-open |
| Flaky | google | 3,804 | 2,902 | 23.7% | 3/5, 3/5, 3/5 | improved vs Sep-12 (62%); 27 open breakers on non-flash models |
| Flaky | openrouter | 1,783 | 1,386 | 22.3% | 3/5, 3/5, — | `:free` tier models are the failure concentration |
| Flaky | opencode-zen | 217 | 128 | 41.0% | 0/5, —, 0/5 | **regressed** from 97.9% (Sep-12); probes all fail |
| Flaky | local-llm (Jetson) | 197 | 114 | 42.1% | 0/5, 0/1, 1/5 | explicit routes rejected ("No healthy upstream") although the Jetson serves every model — see finding 4 |
| Flaky | mlx-mac | 155 | 89 | 42.6% | 4/4, 1/1, 1/4 | errors inflated by my capacity-gate tests; functionally healthy |
| Broken | synthetic | 5,705 | 985 | 82.7% | 0/5, 4/5, 4/5 | text failures are HTML pages on `hf:` routes (1 is a deliberate Kimi-K3 blacklist rejection); vision/tools work |
| Broken | groq | 273 | 42 | 84.6% | 0/1, —, 0/1 | circuit-open on gpt-oss-20b; unchanged since Sep-12 |
| Broken | workers-ai | 2,499 | 115 | 95.4% | 0/5, 0/5, — | free tier still exhausted; 24 open breakers |
| Dead | apim | 162 | 0 | 100% | 0/5, —, 0/5 | upstream 502 `error code: 502`; ClientConnectorError on probes |
| Dead | github-copilot-enterprise | 23 | 0 | 100% | 0/5, 0/5, 0/5 | 12 open breakers |
| Dead | github-copilot | 13 | 0 | 100% | 0/5, 0/5, 0/5 | 3 open breakers |
| Dead | openai | 1 | 0 | 100% | — | single failed call; no traffic otherwise |

Zero-traffic providers (catalog only, no chat traffic): arcee, arliai,
cerebras, cohere (chat), jina, nvidia (3/3 today), voyage.

## Live probe detail (2026-09-22 15:33 AEST)

217 probes: 82 text, 51 vision, 72 tools, 8 embed, 4 rerank.
95 ok / 122 failed. Of the failures: 49 upstream HTML/plain-text error
pages passed through, 19 "No healthy upstream" rejections, 12 circuit-open
rejections (probes hit already-open breakers), 9 audit-503, plus timeouts.

| Provider | text | vision | tools | Dominant failure |
|---|---|---|---|---|
| minimax | 5/5 (2.3 s) | 5/5 (5.2 s) | 5/5 (1.9 s) | — |
| opencode-go | 5/5 (2.8 s) | — | 5/5 (2.8 s) | — |
| zai | 5/5 (4.2 s) | 1/1 (2.9 s) | 5/5 (3.9 s) | — |
| ollama-cloud | 5/5 (7.3 s) | 2/2 (11.2 s) | 5/5 (5.8 s) | — |
| mlx-mac | 4/4 (7.0 s) | 1/1 (7.0 s) | 1/4 | capacity gate during parallel probes |
| alibaba | 3/5 (1.8 s) | 3/5 (2.9 s) | 3/5 (12.2 s) | HTML 502 pages on 2 models |
| google | 3/5 (11.3 s) | 3/5 (11.3 s) | 3/5 (1.9 s) | HTML error pages on 2 models |
| openrouter | 3/5 (1.6 s) | 3/5 (2.1 s) | — | `:free` models fail |
| synthetic | 0/5 | 4/5 (4.5 s) | 4/5 (2.9 s) | `syn:large:text` dead |
| xiaomi | 2/2 (4.8 s) | 1/1 (5.6 s) | 2/2 (18.8 s) | — |
| apim | 0/5 | — | 0/5 | upstream 502 (Cloudflare-style page) |
| github-copilot | 0/5 | 0/5 | 0/5 | HTML error page |
| github-copilot-enterprise | 0/5 | 0/5 | 0/5 | circuit open (11 models) |
| groq | 0/1 | — | 0/1 | circuit-open (rejected at gateway before upstream) |
| local-llm | 0/5 | 0/1 | 1/5 | "No healthy upstream" on all but ornith-1.5:9b (Jetson serves them all) |
| openai-codex | 0/5 | 0/5 | 0/5 | HTML error page |
| opencode-zen | 0/5 | — | 0/5 | HTML error page |
| workers-ai | 0/5 | 0/5 | — | HTML error page (free tier) |

### The HTML passthrough problem

29 text + 11 vision + 9 tools failures returned **raw upstream HTML/plain
error pages** (Cloudflare-style `<!DOCTYPE html>… no-js ie6 oldie …` and
bare `error code: 502`). **Corrected after deeper inspection** — two
distinct cases, only one a gateway concern:

- **Cloudflare edge substitution** (`server: cloudflare`, `cf-ray`
  present, ~0.3 s latency): the CDN in front of the upstream answered
  with its own error page because the origin was down. The gateway never
  received a body; the HTML came from Cloudflare, not through the
  gateway. No gateway classification is possible or needed.
- **Upstream 5xx HTML bodies**: these DO trip breakers —
  `_check_response` raises on the upstream status code, and the observed
  breaker state (e.g. 24 open workers-ai keys) is consistent with that.
  The only genuine defect was that relayed bodies landed verbatim in the
  structured error message. **Fixed in commit `a0a4afa`**:
  `_safe_upstream_body` (`passthrough.py`) now collapses HTML/WAF error
  pages to a stable `<upstream_error_page>` signal.

An audit of the gateway's own structured error envelopes found 0 HTML
payloads in gateway-generated messages — its JSON errors were already
clean.

## Embeddings & rerank

| Route | Result | Latency | Note |
|---|---|---|---|
| `openai/text-embedding-3-small` | ok 1/1 | 1.1 s | 1536 dims |
| `openai/text-embedding-3-large` | ok 1/1 | 1.2 s | 3072 dims |
| `cohere/rerank-v3.5` | ok 1/1 | 0.5 s | correct ranking (Paris doc top) |
| `voyage/*` (embed + rerank) | rejected | — | "No healthy upstream model" |
| `jina/*` (embed + rerank) | rejected | — | "No healthy upstream model" |
| `local-llm/nomic-embed-text` | rejected | — | "No healthy upstream"; tag upstream is `nomic-embed-text:latest` |

**voyage/jina gap**: both providers are configured with `rerank_path` and
`embed_path` (config.py lines 684-700) but have **no `models_path`**, so
the gateway has no catalog for them and model validation rejects every
model name before the request leaves the gateway. The intended usage is
the virtual `hermes-reranker` model (per `tests/test_rerank.py`), which
routes to the configured rerank provider. If direct provider-prefixed
rerank/embed should work for voyage/jina, either add a static catalog or
bypass validation for providers with `rerank_path`/`embed_path`.

## Reliability findings

### 1. Audit writer fails under concurrency (new, high impact)

`AuditLogger._append` (tusker_gateway/audit.py) does, per request:
`open(a+b)` → `flock(LOCK_EX)` → `_previous_hash` tail-scan → write →
`fsync` → unlock — on an **NFS-mounted** RWX PVC. With ≥8 concurrent
requests, some appends raise `OSError`; `TUSKER_AUDIT_FAIL_CLOSED=true`
then converts every audit failure into a client-visible 503.

Isolated measurement (`GET /v1/models`, no provider in path, 24 requests
per level, 8 keys rotated):

| Concurrency | ok | audit 503 | failure % |
|---:|---:|---:|---:|
| 1 | 24/24 | 0 | 0.0% |
| 2 | 23/24 | 1 | 4.2% |
| 4 | 24/24 | 0 | 0.0% |
| 8 | 13/24 | 11 | **45.8%** |
| 16 | 6/24 | 18 | **75.0%** |

Pod logs during the first probe burst show 23 × `audit write failed:
OSError` within one second (05:33:14-15 UTC).

Fix options (pick one):
1. Single-writer: serialize all appends through one asyncio queue +
   one thread (no flock contention), keep fsync.
2. In-process `threading.Lock` around `_append` (flock is redundant for a
   single-pod deployment; the PVC is only multi-writer during rollouts —
   `maxSurge 1` means two pods CAN overlap, so keep flock but add a
   process lock and a short retry/backoff for OSError).
3. Set `TUSKER_AUDIT_FAIL_CLOSED=false` as an interim mitigation — loses
   fail-closed guarantees but stops the 503 storm.

### 2. Tool qualification — initial finding corrected: it IS running

**Correction (2026-09-22, verified against the pod):** the original claim
("never completed for any candidate") was wrong — the audit had read the
stale Postgres `model_tool_capability` table (a `migrate_state.py`
snapshot) instead of the runtime store. The gateway reads
`/home/tusker/.hermes/model_tool_capability.db` — a SQLite file on the
RWX/NFS PVC — live, per call. That DB holds 191 rows; 102 passed under
probe version `stream-tool-contract-v1`.

The maintenance loop is enabled and persists
(`TUSKER_QUALIFICATION_MAINTENANCE_ENABLED=true`, interval 6 h, limit
12, pod age > 1 cycle). Actual coverage is thinner than it looks:
407 of 429 privacy-pool candidates are skipped as quarantined (dead
providers in cooldown), so qualification refresh is concentrated on the
~22 live candidates. A targeted run on 2026-09-22
(`python -m tusker_gateway.tool_qualification --pool privacy`) probed 5
candidates: `mlx-mac/ornith-1.5:35b` passed; `mlx-mac/qwen3.8-27b`,
`qwen3.8-27b:latest`, `qwen38-vtest2`, and
`shirdel-coder-9b-claude-fable-5` all failed `no_tool_call`.

`require_tool_qualification: true` is therefore effective, with static
unqualified models admitted as fallback for non-tool requests.

### 3. Circuit breakers healthy but slow to recover

680 tracked breaker keys, 113 open (16.6%). Largest groups: google 27,
workers-ai 24, opencode-zen 14, github-copilot-enterprise 12,
opencode-go 9. Open-state durations observed up to 756,985 s (~8.8 days)
for `github-copilot-enterprise/gpt-5.5` — effectively permanent for dead
providers. Consider an explicit provider-level disable for the dead ones
(the gateway already has `TUSKER_DISABLED_PROVIDERS`).

### 4. local-llm explicit routes rejected although Jetson serves the models

Every explicit probe (`local-llm/qwen3:4b`, `llama3.2:1b`,
`llama3.2:1b-cpu`, `llama3.2:1b-lowctx`, embed `nomic-embed-text`) failed
at the gateway with "No healthy upstream model is currently available",
while `ornith-1.5:9b` (the one model with a persisted capability probe)
works. The Jetson itself is healthy: `GET /api/tags` lists all 13 models
with capabilities, and `qwen3:4b` answers directly. Pool-level routing
also works — `hermes-privacy` served requests via `llama3.2:3b` during
this audit. The gap is specific to explicit-route model validation for
auto-cataloged local providers: models without a persisted capability
probe are treated as unknown/unhealthy. Same root cause as finding 2.

Note: `mlx-mac/qwen3.8-27b` tools route fails the same way ("No healthy
upstream") while text/vision pass. **Update:** the 2026-09-22 targeted
qualification run settled it — the model genuinely does not support the
stream-tool contract (`no_tool_call`, HTTP 200 in ~66 ms), so its tools
route stays excluded. `ornith-1.5:35b` remains the only tool-qualified
mlx-mac model.

## Recommendations (priority order)

1. ✅ **Fix the audit concurrency bug** — DONE, commit `a0a4afa`.
   In-process `asyncio.Lock` serialization + bounded retry with
   exponential backoff; fail-closed preserved (exhausted retries still
   raise). Regression tests in `tests/test_audit_concurrency.py`
   (concurrent chain integrity, transient retry, fail-closed
   exhaustion); full offline suite 1162 passed.
2. ✅ **APIM** — root cause refined: **not a bad key, a bad auth header.**
   Direct probing shows the gateway's `Authorization: Bearer <key>`
   yields 401 `A valid API subscription key is required`, while the same
   key in the `api-key` header yields **400 `max_tokens` is not
   supported** — i.e. APIM authenticated the request and only rejected a
   parameter. The gateway's auth strategies are exactly
   `{bearer, oauth, codex}` (`auth_strategies.py`), with no
   subscription-key strategy, so `apim` (`kind: bearer`) can never
   authenticate. `apim/gpt-5.6-luna` was already removed from
   `TUSKER_POOL_PRIVACY` (commit `a0a4afa`); re-enabling requires
   adding an `api-key`-header authenticator and switching apim's `kind`.
3. **Do NOT blanket-disable the "dead" providers** — the direct-probe
   verdict (below) shows none of them is a dead upstream:
   - **Dead credentials, needs repair**: `openai-codex` (all 3 OAuth
     refresh tokens report `refresh_token_reused`), `openai` (no key
     configured).
   - **Cloudflare WAF-blocked egress**: `groq`, `opencode-zen` (403
     `error code: 1010` at the edge — the API key is never evaluated).
     Fix is egress/IP or user-agent related, not a key rotation.
   - **Alive, model-catalog staleness only**: `workers-ai`,
     `github-copilot`, `github-copilot-enterprise`, `google`.
   Treating these as dead (as the Sep-12 review recommended) hides real
   provenance errors and blocks recovery when quota/WAF conditions
   clear.
4. ✅ **Tool-qualification pipeline** — CORRECTED, not broken: the
   prober runs and persists to the NFS-backed SQLite store the gateway
   reads live. A targeted privacy-pool run completed 2026-09-22
   (see finding 2). Remaining gap: coverage of quarantined candidates
   (by design) and stale rows >24 h; widen `--limit` or run the CLI
   periodically for deeper coverage.
5. ✅ **HTML error bodies in structured messages** — DONE, commit
   `a0a4afa`: `_safe_upstream_body` collapses WAF/CDN error pages to
   `<upstream_error_page>`. Breaker behavior needed no change (upstream
   5xx already trips breakers; Cloudflare-edge pages never reach the
   gateway).
6. ✅ **voyage/jina** — **not broken; the original finding was a false
   positive.** Live probes on 2026-09-22 all return 200 with correct
   payloads: `voyage/voyage-3` (embed), `jina/jina-embeddings-v3`
   (embed), `voyage/rerank-2.5` (correct ranking, Paris doc top-1),
   `jina/jina-reranker-v2-base-multilingual` (rerank), and the virtual
   `hermes-embed`. The handlers resolve backends from
   `embed_path`/`rerank_path` + `provider_api_keys` (`embed.py`
   `_backend_for`), all of which are configured. The earlier
   "No healthy upstream" readings came from the audit's own probe path
   (restricted key / transient cooldown), not from the embed/rerank
   code. No code change needed; the report's "no `models_path`"
   explanation was wrong.
7. **Keep** minimax, xiaomi, zai, ollama-cloud, alibaba, opencode-go as
   the healthy core; mlx-mac as a hardware fallback. For local-llm,
   clear the stale breakers (`qwen2.5:3b`, `qwen2.5:7b`, `qwen3-vl:8b`)
   once the prober refreshes their rows (stale >24 h rows no longer
   count as qualified).

## Dead vs. cooldown classification (verified 2026-09-22)

Method: direct upstream calls issued from inside the gateway pod, built
from the gateway's own provider registry (`base_url` + `chat_path`) and
`provider_api_keys`, **bypassing gateway gating** (no pool selection, no
breaker, no cooldown). The upstream — not the gateway — produced each
verdict. This separates "provider unreachable/broken" from "gateway
declines to call it right now".

| Provider | Direct upstream response | Classification | Root cause |
|---|---|---|---|
| apim | 401 with `Bearer`; 400 `max_tokens unsupported` with `api-key` | **Misconfigured auth header** | Credentials valid; wrong header name. |
| groq | 403 `error code: 1010` | **WAF-blocked** | Cloudflare edge rejection; key never evaluated. Not quota. |
| opencode-zen | 403 `error code: 1010` | **WAF-blocked** | Same as groq. |
| workers-ai | 400 `No such model: ping` | **Alive** | Authenticated and evaluated; only the probe model was unknown. |
| github-copilot | 400 `model_not_supported` | **Alive** | Authenticated; per-model failures only. |
| github-copilot-enterprise | 400 `model_not_supported` | **Alive** | Same as github-copilot. |
| openai-codex | 401 `refresh_token_reused` (3/3 creds) | **Dead credentials** | Refresh tokens already redeemed; needs reauthorization. |
| openai | 401 `You didn't provide an API key` | **Dead credentials** | No key configured for this provider. |
| google | 404 `models/ping is not found` | **Alive** | Authenticated; model-name resolution only. |
| voyage | 404 on chat path | **Alive** | Chat path not exposed; embed+rerank verified working. |
| jina | 422 validation error | **Alive** | Authenticated; synthetic-probe schema mismatch. |

**Verdict: no provider in this audit is a dead upstream.** Observed
failure modes are misconfigured auth (apim), Cloudflare WAF blocking the
cluster's egress (groq, opencode-zen), dead credentials (openai-codex,
openai), and model-catalog staleness. Pool breakers/cooldowns therefore
encode gateway-side symptoms, which is why several providers show
long-lived "open" breakers while answering direct requests normally.

## Appendix

Durable artifacts from this audit:

- Commit `a0a4afa` — audit append hardening (asyncio serialization +
  bounded retry), `apim` removal from the privacy pool, and cloudflare/
  HTML error-body sanitization.
- `tests/test_audit_concurrency.py` — concurrency, transient-retry, and
  fail-closed regression coverage for the audit logger.
- Commit `cbce50d`..`a0a4afa` on `main` — the source of truth for every
  code claim above; `git show --stat a0a4afa` lists the full diff.

The probe sweep (217 results), 626-model inventory, 82-model sample, and
the audit-isolation/concurrency scratch harnesses were temporary audit
tooling and are not retained. The 7-day table is reproducible from
`provider_usage_daily` in the state DB; live figures from the `/admin/*`
endpoints on the running gateway (generation 306, commit `cbce50d` at
audit time).
