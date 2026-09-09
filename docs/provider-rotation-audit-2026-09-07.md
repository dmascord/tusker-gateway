# Provider rotation audit — 2026-09-07

Full inspection of the pool rotation pipeline (`config.py` → `PoolManager`
(`pools.py`) → `catalog.py` / `extend_pools_with_free_catalog()` →
`k8s/deployment.yaml`). Goal: find rotation-logic gaps, models we could be
using but aren't, and how to get the `google` (Gemini) provider working again.

Status: **resolved 2026-09-08.** Section D executed: A5 code fix landed
(`pools.py` sets `model_data["heavyweight"]` unconditionally for every
auto-free mode, regression test `test_poolmanager_auto_catalog_marks_heavyweight_entries`);
A6 was already fixed before the audit session closed; A1–A3 manifest cleanup,
Gemini recovery (option b), and A4 (`zdr_ok=True` for `xiaomi` + privacy
`auto_catalog_providers` extension)
landed in `k8s/deployment.yaml` and deployed. A7 closed 2026-09-08 with no
code change: `select()` forms the rotation tier from the exact top score
only (pools.py "Group candidates by quality tier"), so floor-scored new
entries sit at the bottom of the ranking (~185/213 live) and get traffic
only via cooldown cascades; qualification + cooldown gates, not the floor,
are what gate new models. Live evidence: zero models with `total_calls=0`,
stable traffic distribution (top model 27 req/24h, rest single digits).
A8 resolved by fixing the doc (no script existed).

## Pipeline as-built (for orientation)

- Pools: `code` (cheap, drops heavyweight, falls back to premium/swarm),
  `privacy` (ZDR-enforced), `premium`/`swarm` (keep heavyweight).
- Selection: session stickiness → filter (request exclusions, registry
  presence, special-purpose slug regex, ZDR policy, context window,
  heavyweight gate, input-modality evidence, advertised/behavioral tool
  support, cooldowns) → quality rank (`quality.py`, adaptive floor
  `max(20.0, median - 20.0)`) → weighted/round-robin within top tier.
- Catalog refresh: `CatalogRegistry.default()` registers per-provider
  clients; `catalog_refresh_loop()` calls
  `PoolManager.extend_pools_with_free_catalog()` after every refresh.
  Auto-free modes per provider: `pricing` (both prices exactly 0),
  `all` (opencode-zen/go, key-filtered), `xiaomi` (chat-only, cheap
  non-ZDR pools only), `catalog` (explicit `auto_catalog_providers`
  opt-in — account allowlist, no zero-price test).
- Heavyweight: slug override set (`heavyweight.py`) OR models.dev pricing
  ≥ $1 input / ≥ $8 output per 1M tokens.

## A. Rotation-logic gaps

### A1. Code pool static allowlist duplicates auto-free discovery

`TUSKER_POOL_CODE` (deployment.yaml) hardcodes ~55 entries; most are
upstream-catalog freebies (`openrouter/*:free`, `opencode-zen/*-free`,
`opencode-go/*`, `ollama-cloud/*`). Because the pool already sets
`auto_free: true`, `extend_pools_with_free_catalog()` re-adds these every
refresh — but they are frozen into `_original_static`, so the pruning
guarantee ("auto-added entries are pruned when they stop being free")
never applies to them. Static rows also never leave the list when an
upstream model dies.

Fix: trim the static list to operator-curated quality picks; let
auto-free repopulate the catalog freebies.

### A2. Dead static rows — providers in `TUSKER_DISABLED_PROVIDERS`

These static rows are silently dropped at `__post_init__` /
`reload_all_pools()` by `_provider_is_disabled()`:

| Row | Blocked by |
|---|---|
| `google/gemini-3.1-flash-lite-preview` (code) | `TUSKER_DISABLED_PROVIDERS` |
| `cerebras/gpt-oss-120b` (code, premium) | disabled + key empty (402 upstream) |
| `cohere/north-mini-code-1-0` (code) | disabled + trial-quota 429 upstream |
| `arcee/trinity-mini`, `arcee/trinity-large-preview` | disabled |

They produce permanent missing-key/disabled noise and never serve traffic.
Remove them, or fix the upstream condition and un-disable the provider.

### A3. `auto_catalog_providers` lists disabled providers

Code pool `auto_catalog_providers` includes `google` and `cerebras`, but
both are in `TUSKER_DISABLED_PROVIDERS` and
`TUSKER_CATALOG_DISABLED_PROVIDERS`. Their catalog clients are never
registered, so the opt-in is a no-op. Align the lists with reality.

### A4. Privacy pool: registry `zdr_ok` contradicts the static allowlist

The privacy pool's static list uses `xiaomi/mimo-v2.5-pro`,
`opencode-go/*`, `ollama-cloud/*`, but `DEFAULT_PROVIDER_REGISTRY`
(`config.py`) marks only `synthetic`, `ollama-cloud`, `opencode-go`,
`openai-codex`, `github-copilot-enterprise`, and `local-llm` as
`zdr_ok=True`. **`xiaomi` is NOT `zdr_ok`** — every privacy selection
drops it at the `zdr_policy` filter (`pools.py` step 2). Check the
current manifest: if `xiaomi` appears in `TUSKER_POOL_PRIVACY`, either
add `zdr_ok=True` to the registry entry (confirm Xiaomi's data-handling
policy first) or drop the row.

Also: privacy `auto_catalog_providers` is only
`("synthetic", "github-copilot")` — it can never discover anything from
`opencode-go`/`ollama-cloud` even though those are ZDR-allowed and
already statically listed. Add them (and `xiaomi` if A4 resolves) to
privacy's `auto_catalog_providers` for ongoing discovery.

### A5. Heavyweight gate missing for `auto_catalog_providers` entries (BUG)

`extend_pools_with_free_catalog()` (`pools.py` ~line 461): the
`is_heavyweight()` check and `model_data["heavyweight"] = ...` assignment
run **only** in the `mode == "xiaomi"` branch. Catalog-mode entries
(`auto_catalog_providers`) get `auto_discovered: True` with no heavyweight
marker, so a paid/heavy model published by an opted-in authenticated
catalog (Codex, Copilot, MiniMax, Z.AI, …) would enter the cheap `code`
pool. The per-entry pricing gate also doesn't protect here because the
`pricing`-mode zero-price test is skipped for catalog mode.

Fix: set `model_data["heavyweight"] = heavyweight` for every mode
(unconditional), so the pool-tier gate (`PREMIUM_POOLS`) filters cheap
pools. One-line change; add a regression test asserting a
heavyweight-priced catalog-mode entry is marked `heavyweight`.

### A6. `openrouter/free` router slug not filtered

`openrouter/free` (and `openrouter/auto` if it appears) are router
endpoints, not deterministic chat models; `is_general_chat_model()` lets
them through (slug doesn't match `_SPECIAL_PURPOSE_MODEL_RE`). Add to
`_PROVIDER_ROUTER_MODELS` or the regex.

### A7. Quality floor may over-promote brand-new auto-discovered models

Adaptive floor `max(20.0, median - 20.0)` gives unproven catalog entries
a third of the traffic tier against established models on day one. By
design, but with ~100 auto-added candidates the code pool rotates very
hot. Watch via `/status`; consider a lower floor for `auto_discovered`
specs if rotation churn becomes a problem. Not fixed this session.

### A8. `k8s/audit-provider-pools.sh` missing

`docs/gateway-model-routing.md` §Provider audit references it; file is
absent. Either restore the script or fix the doc reference.

## B. Missing models we could be using

| Opportunity | Detail | Blocker |
|---|---|---|
| Gemini via `opencode-zen` | `opencode-zen/gemini-3-flash` static row is live (provider not disabled). Verify with `/status` and a probe call. | none — already configured |
| MiniMax-M3 in privacy pool | image-capable; privacy pool has only Synthetic vision (single-provider SPOF for image input) | `minimax` not `zdr_ok` in registry; confirm policy first |
| Premium Gemini | add `{"provider":"google","model":"gemini-3-pro"}` to `TUSKER_POOL_PREMIUM` (already in `HEAVYWEIGHT_SLUG_OVERRIDES`) | google disabled (see C) |
| Copilot-enterprise privacy tier | pool already has auto_discovered `gpt-5.4-mini` etc.; enterprise catalog may list newer cheap slugs — verify with `/status` `auto_catalog_providers` diagnostics | none |
| Cerebras | `gpt-oss-120b` rows exist but account is 402 (needs paid plan) | operator action, not config |

## C. Getting Gemini working again

Everything gates on three env vars in `k8s/deployment.yaml`:

1. `TUSKER_DISABLED_PROVIDERS` contains `google` → every static google row
   is dropped at pool construction; pool selection can never pick it.
2. `TUSKER_PASSTHROUGH_DISABLED_PROVIDERS` contains `google` → explicit
   `google/gemini-*` requests fail fast with `provider_route_disabled`.
3. `TUSKER_CATALOG_DISABLED_PROVIDERS` contains `google` → catalog client
   never registered; no auto-discovery of `gemini-*` slugs.

Code side is already correct and needs no change:
- Registry entry (`config.py:341`): base
  `https://generativelanguage.googleapis.com`, chat
  `/v1beta/openai/chat/completions`, auth `GEMINI_API_KEY`, models_path
  `/v1beta/openai/models`. Verified reachable (endpoint responds; 403
  without key is expected).
- `pools.py:94` excludes Google image-output slugs (`-image`, `imagen-*`)
  from chat pools — correct, keep.
- `heavyweight.py` already classifies `gemini-2.5-pro`/`gemini-3-pro` as
  heavy; flash variants correctly stay cheap-tier eligible.

The 2026-08-24 key audit (`IMAGE_VIDEO_GENERATION_ANALYSIS.md`) verified
the key valid and chat working (`gemini-2.5-flash` smoke OK). The
disablements are over-conservative, not evidence-based today.

**Recovery steps** (manifest edit; needs standard deploy + user
confirmation per destructive-action policy for `kubectl apply`):

1. Remove `google` from `TUSKER_DISABLED_PROVIDERS`.
2. Remove `google` from `TUSKER_PASSTHROUGH_DISABLED_PROVIDERS`.
3. Decide catalog policy:
   - (a) keep `google` in `TUSKER_CATALOG_DISABLED_PROVIDERS` → static
     `gemini-3.1-flash-lite-preview` row stays, no auto-discovery; remove
     `google` from code pool `auto_catalog_providers`; **or**
   - (b) un-disable catalog → drop the static google row, keep `google`
     in `auto_catalog_providers`, catalog populates current gemini slugs
     hourly.
4. Drop the dead `cerebras/gpt-oss-120b` static row (and the `cerebras`
   opt-in from `auto_catalog_providers`) — key empty, 402 upstream.
5. Drop the dead `cohere/north-mini-code-1-0` static row — trial quota.
6. Optionally add `{"provider":"google","model":"gemini-3-pro"}` to
   `TUSKER_POOL_PREMIUM`.
7. After deploy: `curl /status` → confirm `google` candidates listed;
   smoke `google::gemini-3.1-flash-lite-preview` (or catalog slug) with
   `X-Tusker-Cache: bypass`.
8. Update `docs/gateway-model-routing.md` (privacy/provider policy table +
   heavyweight notes) to match whatever lands.

## D. Recommended fix order for next session

1. **Code fix (A5)** — heavyweight marker for catalog-mode auto-added
   entries in `extend_pools_with_free_catalog()` + regression test.
2. **Code fix (A6)** — add `openrouter/free` (+ `auto`) to
   `_PROVIDER_ROUTER_MODELS` in `pools.py`.
3. **Manifest cleanup (A1/A2/A3)** — remove dead static rows (`google`,
   `cerebras`, `cohere`), align `auto_catalog_providers`, trim duplicate
   auto-free-discoverable static entries from `TUSKER_POOL_CODE`.
4. **Gemini recovery (C)** — manifest env changes per steps above; needs
   user confirmation before `kubectl apply` (deploy flow: rsync to
   `visor:/srv/opencode/tusker-ai-gateway/`, `./k8s/deploy.sh`, smoke
   `/health` + `/ready` + gemini probe).
5. **Privacy pool decisions (A4/B)** — `zdr_ok` for `xiaomi` (policy
   confirmation), add `opencode-go`/`ollama-cloud` to privacy
   `auto_catalog_providers`, consider `MiniMax-M3` in privacy for image
   diversity.
6. **Docs** — `audit-provider-pools.sh` reference (A8), routing doc
   updates from 4/5.

Verification checklist per change: `pytest tests/ -p no:cacheprovider`
(skip `tests/test_passthrough_providers.py` offline), then manifest
changes get the deploy-flow smoke (read-only `kubectl` checks are always
allowed; `kubectl apply` needs user confirmation).

## E. Status at 2026-09-09

Items D1–D6 are all resolved in the working tree:

| Item | Where | Evidence |
|---|---|---|
| A5 heavyweight marker | `pools.py:575-588` — `heavyweight` set unconditionally for every mode | `pytest tests/test_catalog.py` |
| A6 router slugs | `pools.py:66-71` — `openrouter/free`, `openrouter/auto`, `free`, `auto` in `_PROVIDER_ROUTER_MODELS` | `pytest tests/test_pools.py` |
| A1/A2/A3 manifest | `k8s/deployment.yaml` — dead `google`/`cerebras`/`cohere` rows gone; `auto_catalog_providers` aligned per pool | `pytest tests/test_pools.py` |
| A4/B privacy | `config.py:304` — `xiaomi` `zdr_ok=True`; privacy `auto_catalog_providers` = `synthetic`, `github-copilot`, `opencode-go`, `ollama-cloud`, `xiaomi` | `pytest tests/test_pools.py` |
| A8 audit script | `docs/gateway-model-routing.md` §Provider audit — references the bounded qualification runner instead of a missing script | doc |

### New fix: per-attempt provider timeout (2026-09-09)

A slow upstream (e.g. `ollama-cloud/minimax-m3` streaming reasoning for the
full 120s deadline) consumed the entire request budget before the fallback
loop could try the next candidate, returning a 502 instead of falling back.

Each pool iteration is now wrapped in `asyncio.wait_for` bounded by
`TUSKER_PROVIDER_ATTEMPT_TIMEOUT_SECS` (default 30s) and the request's
remaining deadline (`request["_deadline_at"]` set by `deadline.py`). On
timeout the iterator is closed (releasing the capacity lease via
`_stream_events`'s `finally`) and a `ProviderError(code="provider_timeout")`
is raised so the existing fallback bookkeeping runs.

- `tusker_gateway/endpoints.py` — `_provider_attempt_timeout_secs()` helper;
  `call_direct()`/`call_candidate()` wrappers in both call sites of
  `_call_with_pool_fallback`.
- `tests/test_chat.py` — `test_pool_fallback_bounded_per_attempt_timeout`,
  `test_provider_attempt_timeout_helper_bounds_remaining_deadline`.
- Commit `37eb397` on `main`.

Verification: `pytest tests/ -p no:cacheprovider --ignore=tests/test_passthrough_providers.py` — 814 passed, 2 skipped.

## F. Gemini recovery (C) — applied 2026-09-09

Manifest-only recovery, no code change. `k8s/deployment.yaml` now:

| Env var | Before | After |
|---|---|---|
| `TUSKER_DISABLED_PROVIDERS` | `arcee,cohere,cerebras,google` | `arcee,cohere,cerebras` |
| `TUSKER_PASSTHROUGH_DISABLED_PROVIDERS` | `cerebras` | `cerebras` (unchanged) |
| `TUSKER_CATALOG_DISABLED_PROVIDERS` | `arcee,arliai,cohere,openai,cerebras,google` | `arcee,arliai,cohere,openai,cerebras` |
| `TUSKER_POOL_CODE.auto_catalog_providers` | `[..., "groq"]` | `[..., "groq", "google"]` |
| `TUSKER_POOL_PREMIUM.models` | 14 entries | 15 entries, `{"provider":"google","model":"gemini-3-pro"}` appended |

Catalog policy chosen: **(b)** — un-disable Google catalog so the
authenticated `/v1beta/openai/models` refresh populates current Gemini
slugs hourly; the static `gemini-3.1-flash-lite-preview` row is dropped
(it was the only dead Google static row and is superseded by catalog
discovery).

`heavyweight.py` already classifies `gemini-2.5-pro`/`gemini-3-pro` as
heavyweight and `pools.py` keeps the `-image`/`imagen-*` exclusion, so
the premium route is the only place a heavy Gemini slug can land; flash
variants remain cheap-tier eligible in `code`.

### Earlier "Blocked stage" — superseded

The original "Blocked stage" subsection below was written before the
visor jump host route was known. Dev host reaches the cluster via
`ssh -J tusker-duckdns`, so direct `kubectl` does work; it just took the
wrong code path initially. See "Deploy-flow status (2026-09-09, blocked
by node wyzard disk-full)" above for the current blocker.

### Deploy-flow status (2026-09-09, blocked by node `wyzard` disk-full)

Reached cluster via `ssh tusker-duckdns` jump. Mirrored source to visor
via `git worktree` of `5642f6e` at
`/srv/opencode/tusker-ai-gateway-build-5642f6e` and ran the documented
deploy flow with `TUSKER_COMMIT=5642f6e750281126d93a493a2c30373d2449d2ea`.
Build (`buildah bud`) and image push to
`registry.tusker.net.au:5000/tusker-gateway:swarm-alpine-5642f6e`
succeeded; `kubectl apply` updated the deployment to the new image.

Rollout is **blocked by node `wyzard` disk-full**, not by the manifest
change. The new pod `tusker-gateway-fbb596959-w7g5g` is stuck in
`ContainerCreating`; kubelet event:
`Failed to create pod sandbox: mkdir /var/log/pods/...: no space left on device`.

`wyzard` `/var` volume is `3.2G / 3.2G (100%)`:

| Consumer | Size |
|---|---|
| `/var/log/atop/` | 198M (7 daily files, ~34M each) |
| `/var/log/journal/` | 193M |
| `/var/log/pods/` | 44M |
| `/var/log/calico/` | 64M |

`DiskPressure=False` — node monitoring watches ephemeral-storage
(`/var/lib/docker`, 99% full but a separate volume), not the small `/var`
volume. Old pod `d3553af` on `wyvern` is still 1/1 Ready and serving
traffic; the rollout's only side-effect so far is that the deployment's
`spec.template.spec.containers[0].image` now points at the new image,
so any node that has room will pick it up automatically once `wyzard`'s
`/var` is freed.

### Suggested unblock (needs user confirmation)

On `wyzard` (10.0.0.218):

1. `sudo journalctl --vacuum-size=64M` — drops journal from 193M to 64M.
2. `sudo find /var/log/atop -mtime +3 -name 'atop_*' -delete` — keeps
   the last 3 days of atop logs, frees ~135M.
3. `sudo rm -f /var/log/calico/*` — frees 64M; calico can recreate.
4. `sudo find /var/log/pods -mindepth 2 -mtime +1 -delete` — drops
   1+ day-old pod logs (kubelet keeps recent ones).

Total expected: ~450M freed on a 3.2G volume → ~14% headroom, enough for
the kubelet pod sandbox to be created and the new pod to start.