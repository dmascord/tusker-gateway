# Gateway model routing

How the gateway picks a (provider, model) pair when a client sends a virtual
alias like `hermes-code`, `hermes-privacy`, `hermes-premium`, or `hermes-swarm`.

## Pool tiers

The gateway has four pools, each with a different tier of models:

| Pool | Tier | Heavyweight allowed? | ZDR enforced? |
|---|---|---|---|
| `code` | Cheap baseline with explicitly configured paid fallbacks | No (dropped) | No |
| `privacy` | ZDR-safe cheap tier | No (dropped) | Yes |
| `premium` | Paid tier | Yes (kept) | No |
| `swarm` | Local / self-hosted | Yes (kept) | No |

The privacy pool is intentionally not a manually curated fallback list. Its
deployment configuration starts with no static models and discovers only
approved ZDR providers. Every discovered privacy candidate must first pass the
current behavioral tool-call qualification before it is selectable, including
requests that do not contain tools. This keeps catalog metadata, stale manual
entries, and models that merely claim tool support from entering the privacy
pool.

### Provider deployment allowlists

Provider-native catalogs may expose more model names than the account actually
provisions. The registry supports `catalog_models` for provider metadata, but a
pool must opt into catalog discovery separately. APIM's `/models` endpoint is a
broad Azure Foundry catalog rather than an inventory of this gateway's deployed
routes, so APIM is not an automatic privacy-pool catalog provider. The privacy
pool keeps the explicitly configured `apim/gpt-5.6-luna` route; the provider's
`gpt-6-luna` route remains available through explicit passthrough.

The tier is the source of truth — `PoolManager.pool_keeps_heavyweight()` returns
`True` for `premium`/`swarm` and `False` for `code`/`privacy`. Callers can override
per-request by passing `heavyweight_ok=True/False` to `PoolManager.select()`.

### Operator latency deny-list

The deployment's `TUSKER_BLACKLISTED_MODELS` removes routes from every pool,
including auto-catalog candidates. On 2026-10-14, 14-day Prometheus latency
data and the retained gateway logs showed no repeatable hour-of-day relief for
the chronically timing-out candidates, so time-based demotion would not reduce
timeouts. These routes are therefore denied continuously until upstream health
is re-established:

- `alibaba/qwen3.8-flash`
- `alibaba/deepseek-v4-flash-0731`
- `ollama-cloud/glm-5.3-flash`
- `synthetic/hf:zai-org/GLM-5.3-Flash`
- `xiaomi/mimo-v2.5-pro`


### Ollama Cloud retirement policy

Ollama Cloud announced retirement of `minimax-m2.7`, `mistral-large-3:675b`,
and `nemotron-3-nano:30b` on 2026-11-09. Production blacklists those exact
`ollama-cloud/*` routes so stale DB pool rows and explicit provider routes fail
before dispatch. Current catalog replacements observed on 2026-10-30 are
`minimax-m3`, `mistral-large-4`, and `nemotron-3-super`; catalog discovery may
also add other supported models. Historical migration files and dated test
fixtures are intentionally left unchanged.

## Heavyweight classification

A model is **heavyweight** when *either*:

1. Its slug is in `tusker_gateway.heavyweight.HEAVYWEIGHT_SLUG_OVERRIDES`, or
2. Its pricing (per 1M tokens) exceeds `cost_input >= $1` OR `cost_output >= $3`
   AND pricing data is available from models.dev.

| Family | Heavy slugs |
|---|---|
| OpenAI Codex | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.5`, `gpt-5.4`, `gpt-6-astra` |
| Anthropic Claude | `claude-sonnet-4.6`, `claude-opus-4.6`, `claude-sonnet-4.5`, `claude-opus-4.5` |
| Google Gemini | `gemini-2.5-pro`, `gemini-3-pro` |
| Cohere | `command-a-plus-05-2026`, `command-a-03-2025` |
| Other | `mistral-large-3:675b`, `deepseek-v4-pro` |
| Synthetic.new (usage-heavy) | `Kimi-K3`, `syn:large:vision` |
| Ollama Cloud (usage-heavy) | `kimi-k2.6`, `kimi-k2.7-code`, `kimi-k3` |
**Ollama Cloud pricing overlay.** Neither ollama's `/v1/models` payload nor
models.dev carries cost data for ollama-cloud, so pricing-based detection
would classify every model as cheap. `OLLAMA_CLOUD_PRICING`
(`tusker_gateway/catalog.py`) overlays ollama.com's published per-1M-token
prices onto catalog entries. Under the $1/$3 thresholds this marks
`glm-5.1`, `glm-5.2`, `glm-5.3`, `kimi-k2.6`, `kimi-k2.7-code`, `kimi-k3`, and
`nemotron-3-ultra` heavyweight by pricing. Slug entries also cover
`kimi-k2.6`, `kimi-k2.7-code`, `kimi-k3` (synthetic), and `Kimi-K3` so static
pool lists (no cost data) stay filtered. `glm-5.3-flash`, `deepseek-v4-flash`,
and the cheap minimax variants stay cheap-tier. Slugs with no published price
stay non-heavyweight (conservative default).

**Not** in the override set (cheap-tier codex slugs):

- `gpt-5.6-luna` (cheapest codex slug, kept in privacy)
- `gpt-5.4-mini` (kept in privacy)
- All `:free` OpenRouter slugs (e.g. `openai/gpt-oss-20b:free`)

To override per-entry (force a normally-light model to be heavy or vice versa),
add `"heavyweight": true|false` to the entry in `TUSKER_POOL_*`:

```json
{"provider": "openai-codex", "model": "gpt-5.6-luna", "heavyweight": true}
```

## Selection algorithm

For each request to `hermes-code`/`hermes-privacy`/etc:

1. **Session stickiness** (if a session_id is provided): reuse the
   previously-selected (provider, model) if it's still a candidate.
2. **Filter candidates** by:
   - Excluded set (per-request, e.g. previous failed fallback)
   - Provider not in registry (skip — avoids ProviderError cascade)
   - Context window too small for the request
   - **Heavyweight filter** (cheap-tier pools drop heavyweight entries)
   - **Privacy provider policy** (`zdr_ok`) for the privacy pool
   - Cooldown active for this (provider, model)
   - Required input modalities (for example, image requests skip known text-only models)
   - Behavioral tool qualification for the privacy pool (a passing current
     streaming tool contract is required even for ordinary chat)
   - Composite image-plus-tool qualification when an image-bearing request
     also supplies tools (see below)
   - ZDR + EXCLUDED_PROVIDERS env var (privacy pool only)
   - Operator model blacklist (`TUSKER_BLACKLISTED_MODELS`)

For an image-bearing request that also supplies tools, selection requires the
model's latest `input_image_tools` capability record to be `passed` with probe
version `image-tool-contract-v2`. Image-only and text-tool qualification are
not combined implicitly: a route that passes only one half is excluded before
the upstream call. The same requirement is applied on ordinary selection,
fallback attempts, recovery probes, and session-sticky re-selection.
3. **Rank by quality score** (descending) from `model_quality.db`. New models
   use an adaptive floor (median - 20.0 clamped to 20.0). Models with an LLM
   Stats rank receive an additional score bonus (see
   [LLM Stats rank preference](#llm-stats-rank-preference)) so better-ranked
   models are preferred within their quality band.
4. **Pick the top-ranked candidate** and remember it for the session.

### LLM Stats rank preference

The gateway periodically syncs capability rankings from
`api.llm-stats.com` (`TUSKER_LLM_STATS_API_KEY` enables it; a daily
background task plus the one-shot
`python -m tusker_gateway.tools.sync_llm_stats` importer). The sync stores
per-model verdicts in `llm_stats.db`:

- **pass** — its best rank across the tracked category windows is within
  the cutoff (`TUSKER_LLM_STATS_MAX_RANK`, default 25). Selection prefers
  these.
- **excluded** — present in a tracked window but best rank beyond the
  cutoff (`TUSKER_LLM_STATS_MAX_RANK`, default 25). What that means is
  controlled by `TUSKER_LLM_STATS_ENFORCEMENT`:
  `drop` (default) removes the model from every pool like a blacklist
  entry; `prefer` keeps it selectable but orders it behind better-ranked
  models, so traffic uses the strongest model that still has quota and
  health and falls through to weaker ones only when the stronger ones
  are cooling down, capacity-gated, or breaker-tripped.
- **unknown** — absent from every top-50 window (private, local, or
  simply outside the rankings windows; fails open; selectable, no bonus).

Evidence comes from the aggregated rankings windows only. One
`GET /rankings?category=<cat>&limit=50` request per tracked category,
so a sync costs five requests. Upstream caps ``limit`` at 50
(returns 422 above it) and ignores ``offset`` / ``page`` parameters,
so the windows cannot be paginated to look further down the leaderboard.
The per-model detail endpoint is deliberately unused: its ranks are
per-benchmark leaderboard positions, not cross-model category positions,
so they are not comparable with the cutoff. Consequence: only models
inside a tracked top-50 window can be excluded; weaker-than-window
models stay `unknown` and selectable.

#### Website seed provenance

Models ranked below the top-50 windows (e.g. `gpt-oss-20b`) can be seeded
from the llm-stats.com homepage, which publishes the same category
leaderboards to depth ~375 as server-rendered data. The seed is an
explicit operator action, never automatic:

    # Compute without writing (safe to run anytime):
    python -m tusker_gateway.tools.import_llm_stats_seed --dry-run
    # Import (fetches 5 API windows for anchoring + the homepage):
    python -m tusker_gateway.tools.import_llm_stats_seed
    # Deterministic/offline: read a saved homepage snapshot instead of HTTP:
    python -m tusker_gateway.tools.import_llm_stats_seed --html page.html

Mechanics and guarantees:

- The homepage leaderboards are parsed from named `category_id` keys and
  accepted only when the site key matches a tracked category **and** the
  ordered top-50 agrees with the API window (>=90% positional overlap,
  `MATCH_MIN_OVERLAP`), or when an array matches the window exactly under
  a different key. If the site changes shape, the affected category is
  skipped — a wrong verdict is not producible from a mismatch.
- Seed rows carry `evidence=site_seed` and are written only for pool
  models **absent from every API window**; rows with API window evidence
  are never overwritten. API evidence always wins on the next sync.
- Seeds age out: the daily sync carries a `site_seed` row forward only
  while it is younger than `TUSKER_LLM_STATS_SEED_MAX_AGE_SECS`
  (default 7 days); after that the model degrades back to `unknown`
  unless the seed is re-imported. Set the variable to `0` to disable
  retention entirely.
- A seeded `excluded` verdict behaves exactly like a window-derived one:
  a deep rank beyond the cutoff drops the model from its pools (`drop`)
  or orders it last (`prefer`), per `TUSKER_LLM_STATS_ENFORCEMENT`.

Coverage source: `docs/llm-stats-coverage-2026-09-27.md`.

#### Strength probe for unknown models

Models absent from every llm-stats window (aggregator aliases like
`opencode-cli/big-pickle`, private models like `synthetic/syn:*`) are
`unknown` and select without a rank bonus — below every ranked model,
even weak ones. The strength probe measures them on the same yardstick:

    # Report without writing (safe to run anytime):
    python -m tusker_gateway.tools.probe_strength --dry-run
    # Probe every unknown pool model and write probe verdicts:
    python -m tusker_gateway.tools.probe_strength
    # Explicit targets/references:
    python -m tusker_gateway.tools.probe_strength \
        --models opencode-cli/big-pickle \
        --refs zai/glm-5.3-flash,groq/openai/gpt-oss-20b

It sends a deterministic, mechanically graded question bank (multi-step
arithmetic, multiple choice, code-output prediction, strict JSON
instructions; temperature 0) through the gateway's own
`provider/model` passthrough. Reference models with known ranks are
probed with the same questions, producing a (score, rank) calibration
curve; each probed model's ladder position is interpolated from its
score. Results are written with `evidence=probe`: window and site-seed
evidence always win, re-running the probe refreshes estimates, and
probe rows age out after 14 days (`--expire-age`) unless refreshed.
Never edit the question bank without re-probing every reference model —
scores are only comparable within one bank generation.

Ranked models additionally receive a quality-score bonus at selection
time: `max(0, cap - per_rank * (rank - 1))`, with `cap = 25.0` and
`per_rank = 1.0` by default (`TUSKER_LLM_STATS_RANK_BOOST_CAP`,
`TUSKER_LLM_STATS_RANK_BOOST_PER_RANK`; set per_rank to 0 to disable).
Under `TUSKER_LLM_STATS_ENFORCEMENT=prefer` an unset per_rank instead
defaults to `cap / (PREFER_LADDER_SPAN - 1)` (`PREFER_LADDER_SPAN=200`,
so ~0.126 with the default cap): the decay reaches zero only at rank
200, keeping the whole ranked range ordered so fall-through follows the
ranking. The drop-mode default (per_rank = 1.0, zero by rank ~26) is
unchanged.

Consequences of the bounded default:

- Among equally-scored (e.g. both primed) candidates the better rank wins
  deterministically instead of round-robin.
- The bonus can never lift a model above a far healthier one: a rank-1
  model with 0% learned success (`quality 2.0 + 25 = 27`) still loses to
  a healthy unranked model (`quality ~100`). Learned failure evidence
  always dominates.
- Auto-catalog candidates participate too: they share the same
  `llm_stats.db`, so discovered models with good ranks rise inside their
  pool without operator action.

Set `TUSKER_LLM_STATS_RANK_BOOST_PER_RANK=0` to restore pure
quality-score ordering in either mode.

If a pool has `fallback_pools` configured, the same request requirements are
applied to each fallback pool after the current pool has no eligible
candidates. This is explicit in configuration so `hermes-code` can fall back
to paid or self-hosted capacity without changing the privacy pool's routing
policy. The gateway still returns a retryable 503 when every configured pool
is unavailable; no routing policy can bypass an upstream quota or outage.

When all candidates are temporarily excluded by individual model/provider
cooldowns, request fallback performs one bounded recovery probe. It keeps
provider registration, privacy policy, input modalities, known advertised tool
support, and shared/global capacity quarantines enforced, while allowing a
stale transient cooldown to be retried. Tool requests may also use a curated
model with a structured but non-strict qualification result during this probe;
the stream boundary still validates and sanitizes the complete tool response
before sending it to the client. If that probe still has no tool route, a second
bounded compatibility probe may try curated static models whose persisted
capability result is stale or says tools are unsupported; auto-discovered
models remain gated. This is a last-resort availability measure, not a
capability claim: the response validator remains the hard boundary and failed
calls are quarantined normally. `/status` exposes the active cooldown scopes
and pool state for diagnosing the remaining cases, including missing keys.

### Hindsight structured-output qualification

Hindsight is configured to call `hermes-privacy`, so its JSON retention and
consolidation requests stay inside the privacy policy. When a privacy-pool
request includes `response_format` of `json_object` or `json_schema`, the
gateway prefers candidates with a recent exact JSON-schema probe. A recent
explicit rejection is excluded; an unavailable or untested candidate remains
eligible so a provider can recover or be qualified on first use.

The background qualification task refreshes a small batch every six hours,
probing never-tested candidates first, then the oldest evidence. Each cycle
refreshes provider catalogs and uses the same expanded pool for probing and
coverage. Degradation means fewer than two currently selectable privacy routes
have fresh passing evidence; cooldowns, privacy policy, and heavyweight gates
still apply. Counting coverage does not advance traffic rotation.

Probe version `structured-output-v2` requires exactly `{"ok":true}` with an
actual JSON boolean: numeric `1` and extra properties fail. Earlier probe
versions are not reused as passing evidence. The standalone check is:

```bash
tusker-gateway-qualify-structured --pool privacy --limit 4
```

Only status, latency, HTTP status, failure class, probe version, and timestamp
are stored in the model-capability database; probe prompts and response bodies
are not retained.

## Dynamic catalog refresh

The gateway pulls live model catalogs from upstream providers at runtime to
surface eligible models without requiring a config edit:

| Provider | Endpoint | TTL |
|---|---|---|
| Codex | `https://chatgpt.com/backend-api/codex/models?client_version=0.0.0` | 60 min |
| GitHub Copilot | `https://api.githubcopilot.com/models` | 5 min |
| OpenRouter | `https://openrouter.ai/api/v1/models` | 60 min |
| OpenAI-compatible providers | Provider-configured `models_path` (for example `/v1/models`) | 60 min |
| models.dev | `https://models.dev/api.json` (pricing DB) | 60 min |
| Xiaomi MiMo | `https://token-plan-sgp.xiaomimimo.com/v1/models` | 60 min |
| OpenCode CLI | `opencode models` subprocess (pinned CLI; `opencode/` namespace is the free Zen set) | 60 min |

Static `TUSKER_POOL_*` entries remain operator-curated baselines. Pools with
`auto_catalog: true` also receive eligible catalog entries at startup and on each
refresh. Catalog-only entries are pruned when they disappear or become
ineligible; static entries are never pruned.

`TUSKER_AUTHORITATIVE_CATALOG_PROVIDERS` optionally lists providers whose
catalog is complete enough to exclude missing routes. It defaults to empty.
For opted-in providers, a successful, current, nonempty snapshot gates
selection (including sticky sessions), status, and readiness. Configured
`model_aliases` are resolved before lookup. Failed, stale, or empty snapshots
leave the configured baseline eligible; static configuration is not deleted.
Catalog presence does not prove account entitlement.

The first upstream 404/410 excludes that provider/model from pool rotation
indefinitely, for both streaming and non-streaming chat. Normal selection,
sticky sessions, recovery probes, status, and readiness respect the marker;
catalog refresh cannot re-add it. `TUSKER_RETRY_PERMANENT_COOLDOWN` no longer
sets an expiry for these markers. They remain process-local: restarting the
gateway clears them. Explicit `clear_permanently_failed(provider, model)` or
the existing successful explicit-request path can clear a marker; explicit
provider/model requests bypass pool selection and can still probe the route.
Google requests omit the unsupported OpenAI `store` field after request
fields are merged.

## Auto-free catalog merge

Pools can opt in to **automatic free-tier discovery** by setting
`auto_catalog: true` in their `TUSKER_POOL_<name>` JSON env var:

```json
TUSKER_POOL_CODE='{"models": [], "auto_catalog": true}'
```

Account-backed catalogs can be opted in explicitly with
`auto_catalog_providers`. This is intended for subscription or credited
providers whose catalog does not expose a meaningful zero-price signal:

```json
TUSKER_POOL_CODE='{"models": [], "auto_catalog": true, "auto_catalog_providers": ["openai-codex", "github-copilot", "github-copilot-enterprise", "opencode-go", "zai", "synthetic"]}'
```

These entries are still filtered by heavyweight and privacy policy. A
catalog entry is not eligible for a tool-bearing request until the bounded
qualification runner records a passing structured stream; an unqualified
entry may only be used by routes that do not request tools.

When enabled, every catalog refresh (initial + 5-minute background loop)
walks the registered catalogs and merges any model currently available for
free into the pool's allowlist. Discovery differs per upstream:

| Upstream | Free-tier signal |
|---|---|
| `openrouter` | `pricing.prompt == "0" AND pricing.completion == "0"` |
| `arcee` | Provider-native model catalog; static code route uses `trinity-mini` |
| `opencode-zen` | All entries returned by `/zen/v1/models` (the upstream key-filters paid models) |
| `opencode-go` | All entries returned by `/zen/go/v1/models` (same key-filter) |
| `xiaomi` | Authenticated Token Plan catalog; proven chat models only, cheap non-ZDR pools only, heavyweight entries excluded |
| provider-native catalogs | Both catalog/model.dev prices must resolve to exactly zero; unpriced or paid models stay out of free pools |
| `opencode-cli` | Every model in the `opencode/` namespace of the pinned CLI's `opencode models` output — the CLI's own eligibility list. The key-filtered HTTP `/zen/v1/models` catalog omits CLI-exclusive free models, so it is not used as a filter |

Providers listed in `auto_catalog_providers` use their authenticated catalog
as the account's allowlist rather than the zero-price test. This is the
explicit path for Codex, Copilot, OpenCode Go, Z.AI, Synthetic, MiniMax,
Ollama Cloud, Groq, Google, or Cerebras when their credentials provide model
access.

`opencode-cli` is registered only when `TUSKER_OPENCODE_CLI_ENABLED` is set;
the code pool opts in through `auto_catalog_providers`, so new free CLI
models enter rotation and get tool-qualified without a manifest change. The
static `opencode-cli` entries in `TUSKER_POOL_CODE` remain as the cold-start
floor (they survive a missing CLI binary; auto entries do not).

`TUSKER_AUTO_CATALOG_EXCLUDED_PROVIDERS` can block a provider from dynamic
catalog discovery while leaving explicitly configured models untouched. The
deployment sets it to `nvidia` while that provider's worker capacity is under
investigation; this prevents a direct NVIDIA `/models` refresh from adding new
routes automatically.

## Provider audit

Run the read-only audit on the cluster build host after a deployment:

```bash
# There is no bundled audit script. Use the bounded qualification runner
# (tusker_gateway/tool_qualification.py) against the gateway, then inspect
# catalog diagnostics from the authenticated /status endpoint:
kubectl -n hermes exec deploy/tusker-gateway -- \
  curl -s -H "Authorization: Bearer $TUSKER_API_KEY" http://localhost:8642/status \
  | jq '.catalog'
```
Qualification results land in the gateway's persistent tool-capability
database; response bodies and credentials are not retained.

### Media capability evidence and generation probes

Embedding and rerank qualification records only bounded route-level evidence. A
passing embedding result must contain non-empty finite vectors with consistent
dimensions; a passing rerank result must contain a non-empty result list with
finite numeric relevance scores. This proves the configured route's response
contract, not broad model quality or every supported input shape.

Image-input qualification similarly proves that one small decodable PNG is
accepted by the selected chat route. It does not prove OCR, multiple images,
large images, audio/video input, or generation behavior. Results are persisted
without response bodies or credentials and are versioned, so old evidence is
not treated as current proof.

The composite image-plus-tool probe is intentionally explicit and bounded:

```bash
tusker-gateway-qualify-image-tools --model PROVIDER/MODEL
```

It sends one tiny image and one harmless required tool call. The required tool
argument includes the observed middle-stripe color, and the gateway compares it
with the known probe image as well as validating the path, tool name, and stream
completion. This prevents a text-only route from passing merely because
`tool_choice=required` forced a syntactically valid call. It never changes pool
membership or enables a route automatically. Generation probes are likewise
opt-in: run them only for explicitly selected providers/models because

When `TUSKER_COMPOSITE_QUALIFICATION_ENABLED=true`, the background maintenance
loop refreshes a bounded batch of lightweight chat routes every
`TUSKER_COMPOSITE_QUALIFICATION_INTERVAL_SECS` (default 12 hours). It only
persists route-level evidence and never admits a route from image support or
ordinary tool support alone. The batch size, timeout, and evidence age are
controlled by `TUSKER_COMPOSITE_QUALIFICATION_LIMIT`,
`TUSKER_COMPOSITE_QUALIFICATION_TIMEOUT_SECS`, and
`TUSKER_COMPOSITE_QUALIFICATION_MAX_AGE_SECS`.


The privacy pool applies the provider policy before catalog pricing. The
default registry currently allows local `local-llm`, Ollama Cloud, OpenCode
Go, OpenAI Codex, GitHub Copilot Enterprise, Xiaomi MiMo, and the public
Copilot route (via the `TUSKER_COPILOT_BUSINESS` opt-in below). The strict
privacy pool discovers Ollama Cloud as a ZDR-eligible provider; models such as
`glm-5.3-flash` still require a passing behavioral qualification before use.
Deployment
overrides register the local hardware routes explicitly; runtime provider
configuration takes precedence over built-in endpoint defaults.
Public GitHub Copilot,
OpenRouter, NVIDIA trial endpoints, and other direct providers remain outside
the privacy pool unless an explicit deployment policy enables them.

### Local hardware routes

`k8s/deployment.yaml` sets these endpoints in `PROVIDER_REGISTRY_JSON`:

| Provider prefix | Backend | Catalog |
|---|---|---|
| `local-llm/` | Jetson Orin Nano Ollama, `http://10.0.0.212:11434` | `/api/tags` |
| `mlx-mac/` | Mac M4 Max (Ollama + MLX Metal backend), `http://10.0.0.141:11434` | `/api/tags` |

Both use `/v1/chat/completions` and never substitute a gateway pool. The MLX
host runs Ollama with Apple MLX (Metal GPU) as its inference backend; there
are no separate `mlx_lm`/`mlx_vlm` servers. Use these client-facing names:

```text
local-llm/qwopus-9b-coder-mtp:latest
mlx-mac/qwen3.8-27b
```

Both `local-llm` and `mlx-mac` advertise their catalog via the gateway's
`/v1/models`. Unmapped model IDs pass through unchanged. Jetson's chat models
are deliberately not in the normal chat pools; its role is local embeddings.
The MLX route remains governed by the configured chat pool and its quality
and rotation logic.

#### Jetson embedding service

`POST /v1/embeddings` tries `local-llm/nomic-embed-text` first (768 dimensions)
using Ollama's `/v1/embeddings` API. It is a small 137M-parameter model and
is kept separate from chat-pool routing. If the Jetson is unavailable, the
priority-ordered fallback is Synthetic, Voyage, Jina, then OpenRouter, subject
to each provider having an API key and being healthy. The fallback behavior
preserves availability but sends embedding input to the selected remote
provider; callers requiring local-only processing can pin
`model: "local-llm/nomic-embed-text"`, which does not fall through to remote
providers. `TUSKER_EMBED_STRATEGY=priority` makes configured order deterministic;
the default for other deployments remains round-robin.

#### Single-concurrency capacity gate

Both `local-llm` and `mlx-mac` are isolated into their own capacity groups
(`local-llm` and `mlx-mac`) and run at
`TUSKER_LOCAL_LLM_MAX_CONCURRENT=1` / `TUSKER_MLX_MAC_MAX_CONCURRENT=1`.
The Jetson Orin Nano and the MLX Mac both saturate RAM and CPU while
cold-loading a 7B-class model; queueing concurrent requests just stalls all
of them, so the gateway fails fast to the next pool candidate (or returns
`503 service_unavailable` with `Retry-After: 5` for explicit routes) instead
of holding the request open while the local backend thrashes.

Per-attempt idle budget for these providers is `240s` (raised via
`TUSKER_PROVIDER_ATTEMPT_TIMEOUT_OVERRIDES_JSON={"local-llm": 240,
"mlx-mac": 240}`). Alibaba reasoning requests use a model-specific `120s`
override for `alibaba/qwen3.8-max`; the request deadline remains the hard cap.
Cloud candidates still return in well under the default deadline; the longer
budget only affects the explicitly slow routes. After a capacity failure the
route is in capacity cooldown (`TUSKER_PROVIDER_CAPACITY_COOLDOWN_SECS=300`)
so a hot backend can finish its current request and the gateway stops
hammering it.

The attempt budget is not the only idle timer on a stream. An upstream read
has two more — aiohttp's `sock_read` and the SSE per-chunk budget — and a
shorter one pre-empts the attempt budget, so the operator-visible timeout
never applies. `passthrough._upstream_idle_secs()` derives both from
`TUSKER_UPSTREAM_SOCK_READ_SECS` (default `90`) raised to the route's attempt
override, then raised again by any
`TUSKER_PROVIDER_STREAM_TIMEOUT_OVERRIDES_JSON` entry. So `mlx-mac` reads at
`240s`, not `90s`, and the documented `xiaomi: 600` stream override is
actually reachable. Production evidence for the fix is
`req_e1d8a9fe5ade8736` (2026-10-21), where a cold 240K-token `mlx-mac` prompt
died at 90s under a 240s attempt budget.

Non-streaming requests wait for the whole completion, so they have no idle
timer to reset and aiohttp's `total` is the only upstream deadline. It must
derive from the same attempt budget: both non-stream call sites use
`passthrough._non_stream_total_secs()`, which returns
`TUSKER_UPSTREAM_SOCK_READ_SECS` raised to the route's attempt override.
A flat `120s` here is the same trap as a flat `sock_read` — before
2026-10-21 the non-stream path passed a literal `120`, so a configured-240s
`local-llm` request failed at 120.4s and again at 121.0s (reproducible)
while the identical request took ~1s warm and ~6s cold directly against
Ollama. Non-stream `local-llm`/`mlx-mac` now get the full `240s`.

#### Process-backed CLI capacity gate

`opencode-cli`, `claude-code-cli`, and `kilo-cli` (only when it is *not*
proxied to the dedicated worker) each run a CLI child process inside the
gateway container for the whole request, so they share one `cli` capacity
group at `TUSKER_CLI_MAX_CONCURRENT=1`. An uncapped burst of concurrent
clients exhausted the 3 GiB pod limit and the kernel OOM-killed the gateway,
taking every other pool down with it — this happened 16 times on 2026-10-14.
The in-flight slot is held across the entire streamed response
(`_lease_released_stream`) and released on completion, failure, or consumer
close; when the group is saturated the gateway fails fast to another
candidate (or `503` for an explicit route) and quarantines the `cli` group
for `TUSKER_PROVIDER_CAPACITY_BUSY_COOLDOWN_SECS` (default 2s) instead of
spawning another child. One slot is intentional: an individual CLI child can
approach a GiB alongside the gateway process and its warm service.

Because these CLIs treat their working directory as the project and load its
file tree and instructions into the model context, every child process runs
in a request-scoped empty directory with `KILO_DISABLE_PROJECT_CONFIG=true`.
Inheriting the gateway's own source tree would leak it into third-party
prompts and make the model's file tools (edit/find) target gateway code,
auto-reject, and fail the request.

#### Declared context windows and max_tokens fitting

Ollama sizes `num_ctx` from host memory unless `OLLAMA_CONTEXT_LENGTH` or a
model's `PARAMETER num_ctx` says otherwise, and ignores `options.num_ctx` on
`/v1/chat/completions`. The pool-wide `context_window` (128k) is only a
guess, so `TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON` declares real windows,
keyed by `provider` or `provider/model` (model keys win; `:latest` is
optional). Prefer model-specific values when only some models on a provider
have been verified.

The MLX Mac runs `OLLAMA_CONTEXT_LENGTH=262144` (set 2026-09-24 in
`/Library/LaunchDaemons/dev.tusker.ollama.plist`; its default was 32k).
The three configured MLX models report native 262144 context. At 128k,
ornith-1.5:35b used 22.2 GiB, qwen3.8-27b 20.6 GiB, and shirdel-coder-9b
9.5 GiB, all on the GPU; 256k is enabled on the 48-GiB M4 Max, but should be
watched for peak memory under concurrent load. Jetson Orin Nano has 8 GiB
total RAM, so the gateway deliberately makes no blanket 128k claim for it.
Only `qwopus-9b-coder-mtp` has a declared 8192 context, matching its
Modelfile; verify actual `ollama ps` allocation before raising other Jetson
models. The duplicate
`homebrew.mxcl.ollama` daemon is disabled; it was crash-looping on the
port held by `dev.tusker.ollama`.

For declared routes only:

- Selection skips the route when the prompt estimate plus
  `TUSKER_MIN_OUTPUT_TOKENS` (default 4096) plus a 256-token margin exceeds
  the window. `pool '<name>' context window filter ...` logs what was dropped.
- `max_tokens` / `max_completion_tokens` is clamped to
  `window - prompt_estimate - 256`. An unset value stays unset.

The prompt estimate (`context_limits.estimate_prompt_tokens`) counts message
text, tool-call arguments and tool schemas at 3.2 chars/token (measured on
the Qwen tokenizer: code ~3.3, schema JSON ~3.4, prose ~5.1), plus
`TUSKER_IMAGE_TOKEN_ESTIMATE` (default 1600) per image. Every attempt logs
`context budget rid=... window=... prompt_estimate=... requested_max_tokens=...
effective_max_tokens=... clamped=...`.


A tool turn whose provider output is unusable is rejected by stream validation. This
includes a complete turn with no answer and no tool call
(`reasoning_only_or_empty`), as well as other unusable tool responses. Such a
turn is eligible for failover in the pre-visible recovery gate: the gateway may
re-dispatch it to alternative candidates while only role/SSE metadata has
reached the client, bounded by `TUSKER_MAX_PREVISIBLE_FAILOVERS` (default 3).
Each retry also spends the shared per-request candidate budget
(`TUSKER_MAX_PROVIDER_ATTEMPTS`), so a pool full of unusable routes cannot
multiply its own retries. The gate refuses to re-route once visible assistant
output has reached the client. Each failed route is quarantined for
`TUSKER_TOOL_FAILURE_COOLDOWN_SECS` (default 300s).

Only if every alternative also fails does the client receive the gateway error
message. Incident: `req_639b33b3904d2d0c` (2026-10-15) on
`alibaba/deepseek-v4.1-flash`; the same route served six requests with
190k-215k-token prompt estimates in the following five minutes, so the empty
turn was not a window or length effect. The rejection-to-failover regression is
covered by `tests/test_chat.py::test_chat_stream_empty_tool_response_falls_back_before_client_response`.

The single alternate this gate originally allowed was still too tight for a
pool served mostly by cheap routes. Incident: `req_15253924bceb5747`
(2026-10-15) on the same route, which streamed a truncated DSML opener
(`<tool`, `_c`, `alls`) as content and stopped with no structured call; the
stripper swallowed the markup, so the turn carried no client-visible bytes and
was rejected as `reasoning_only_or_empty`. Two such turns in a row exhausted the
budget and the client read "the route was temporarily quarantined" although
nothing had been streamed. Multi-candidate recovery is covered by
`tests/test_tool_stream_integrity.py::test_unusable_tool_turns_fail_over_before_client_output`,
which replays the captured upstream bytes for two unusable candidates before a
usable one.

A provider 429 raised mid-stream before any visible output takes the same gate.
`RateLimitError` is not a `ProviderError` (it subclasses `GatewayError` so the
pre-stream handlers can catch it ahead of the generic provider branch), which
meant it missed the committed-stream recovery clause entirely and
`rate_limit_exceeded` was absent from `_STREAM_RECOVERY_CODES`. Both omissions
are fixed: a throttled candidate now yields to the next one instead of handing
the client the terminal "Upstream provider rate limit exceeded; retry shortly."
frame while healthy candidates were still eligible. Covered by
`tests/test_fault_scenarios.py::test_midstream_rate_limit_fails_over_before_visible_output`.
The message still reaches the client when every eligible candidate is
throttled, and the visible-output guard still refuses to splice providers after
assistant bytes have been written.

Every client-visible terminal failure appends `Request ID: <rid>.` so a
truncated turn is traceable in the access log. Rate-limit and provider-capacity
failures were the last branches missing it: the 2026-10-31 privacy-pool report
was a bare "Upstream provider rate limit exceeded; retry shortly." with nothing
to quote. `_provider_rate_limit_response`, the capacity 503, the 502
`provider_error` branch, and the SSE frame from `_public_stream_error` now all
carry the ID; the `provider_error` message told callers to quote a request_id
while omitting it. Covered by
`tests/test_chat.py::test_chat_rate_limit_429_includes_request_id` (the envelope
carries exactly the rid the failure was logged under) and
`tests/test_chat.py::test_public_provider_non_capacity_failure_does_not_leak_upstream_body`.

### Copilot capability shape

GitHub Copilot and GHE publish input modalities and tool support as booleans
under `capabilities.supports` (`vision`, `tool_calls`) rather than the
`architecture.input_modalities` list the other catalogs use. Both
`_advertised_modalities` and `advertised_tool_support` stopped one level short
of that shape, so every GHE row parsed as image-unknown. Because `privacy`
lists `github-copilot-enterprise` in `auto_catalog_providers`, the input-modality
filter dropped all of its routes on image requests and the pool fell back to
`apim/gpt-5.6-luna` alone. Verified against the live
`copilot-api.sita.ghe.com/models` payload: 21 of 31 models resolve image+tool
support where none did before, and the privacy request that had been failing
served a real vision answer.

### Ollama capability shape

Ollama publishes a flat capability vocabulary instead of modality arrays:
`capabilities: ["completion", "thinking", "tools", "vision"]`. Neither
`_advertised_modalities` nor `advertised_tool_support` read that list, so
every Ollama row parsed as image-unknown and the privacy pool dropped all of
its routes on image requests. The visible symptom was the same as the
Copilot case above — the pool fell back to a single candidate, and a 429 on
that candidate emptied the pool and surfaced "Upstream provider rate limit
exceeded; retry shortly.".

Two payload shapes carry the list, and both are now read:

- `mlx-mac` and `local-llm` publish it in `/api/tags`.
- Ollama Cloud's `/v1/models` returns two model lists for the same slugs: a
  sparse OpenAI-style `data` list (`{"id": ...}` only) and a richer `models`
  list carrying `input_modalities`, `visibility` and `supported_in_api`. The
  catalog took the first key present, so the sparse list won and every
  capability claim was discarded. `ProviderModelsCatalog` now picks the first
  collection that actually advertises capabilities; a payload with no such
  collection still falls back to the first one.

Three rules follow from the shape:

- `vision` is the only non-text input modality Ollama publishes, so a chat
  row resolves to `text` or `text+image`. The output direction stays
  unknown rather than becoming an explicit text-only claim.
- A capability list outside the chat vocabulary (`["embedding"]`,
  `["decision"]`) says nothing about input acceptance, so it stays unknown
  instead of being read as text-only.
- An absent `tools` entry is not a denial. The local Ollama runtime omits
  `tools` for `mlx-mac/qwen3.8-27b`, which nevertheless returns a real
  function call, so the tool claim is positive-only: `tools` present means
  supported, absent means unknown.

Rows the provider itself marks unreachable (`visibility: "hide"`,
`supported_in_api: false`) are skipped — they are discovery metadata, not
routes. Verified against the live Ollama Cloud payload: 14 of the 24 served
models resolve image input where none did before, and the hidden
`clef:27b` row is excluded.

Covered by `tests/test_catalog.py::test_catalog_capability_helpers_read_ollama_capability_list`,
`tests/test_catalog.py::test_provider_models_catalog_prefers_collection_carrying_capabilities`
and `tests/test_pools.py::test_ollama_capability_shape_admits_image_tool_privacy_candidates`
(which fails without the parser change).


A tool turn that ends `finish_reason=length` with no answer or tool call is
classified as `output_budget_exhausted`. That depends on the request, not the
model, so the route is never quarantined for this verdict. The client is told
the prompt nearly filled the window. Incident: `req_7b0d460047e30296`
(2026-09-23) sent a 32,669-token prompt to ornith's 32,768-token slot.

Because the output budget is the only request-dependent cause of an empty tool
turn, `output_budget_exhausted` leaves the route alone; other unusable tool
turns are treated as route failures and quarantined.

Operators should expect:

- First request after a cold start of a large model may take ~25s on
  `local-llm` and ~6s on `mlx-mac` once the model is warm.
- Concurrent requests on the same provider receive `503 service_unavailable`
  immediately with `Retry-After: 5`. Clients that ignore `Retry-After` will
  see hard failures where they previously saw slow responses.
- After a capacity failure the route is suppressed for ~5 minutes even
  through explicit `local-llm/...` requests; this is intentional to let
  the upstream worker drain.
- Privacy-pool callers fall back to the next ZDR-eligible provider in the
  privacy pool, never to a non-privacy candidate, so a saturated local
  route degrades to cloud privacy (synthetic, github-copilot-enterprise)
  rather than to a non-privacy provider.

On 2026-09-08, x20-gateway (`10.0.0.1`) received a persistent DHCP reservation
for `10.0.0.141`, name `mac-10-0-0-141`, en0 MAC `be:cf:90:06:f5:f6`.
The Mac retained it across a reboot. Keep this network's private MAC stable;
changing that identity requires updating the reservation. Router procedures
live in the adjacent `openwrt` repository; no static address was set on macOS.


The deployment sets `TUSKER_COPILOT_BUSINESS=true` because its public Copilot
credentials belong to a Copilot Business account. That opt-in marks the public
Copilot route privacy-eligible as well; individual-account deployments should
leave it unset. GitHub documents that Copilot Business and Enterprise customer
data is not used to train AI models, but this gateway policy does not remove
GitHub's service-side processing or content-filtering behavior.

GitHub Copilot Enterprise entries added from the live catalog are marked
`auto_discovered`; they must pass behavioral tool qualification before a
tool-bearing request can use them. A pass requires exactly one matching
function call, valid JSON arguments, and a `tool_calls` finish reason. Normal
assistant text alongside that structured call is permitted; it is recorded as
`structured_stream` rather than the stricter no-prose level. This keeps catalog
metadata from reintroducing malformed or empty tool calls without rejecting
otherwise valid models for harmless preambles.

Xiaomi's catalog does not publish modality metadata. The gateway enriches the
verified current models: `mimo-v2.5` accepts text and image input;
`mimo-v2.5-pro` is text-only. ASR and TTS product slugs are excluded from chat
pools. This metadata is enforced on every pool selection and fallback retry,
including Anthropic-format image requests after conversion.

**Idempotence across refreshes**: entries we auto-added are tracked
separately in `PoolManager.auto_added`. When an entry stops being free on
a subsequent refresh (e.g. `stealth/ox-alpha` flips from pricing 0/0 to
3e-6/1.5e-5) it's pruned without disturbing operator-curated entries.

**Pruning proof** (`tests/test_catalog.py::test_poolmanager_auto_catalog_drops_models_that_stop_being_free`):

```text
pass 1: stealth/ox-alpha is free
        pool.models = [{openrouter, stealth/ox-alpha}]
        auto_added[code] = {(openrouter, stealth/ox-alpha)}
pass 2: stealth/ox-alpha is now paid
        pool.models = []
        auto_added[code] = set()  # pruned
```

The original static config (from the operator's `TUSKER_POOL_*` JSON) is
frozen at `PoolManager.__post_init__` time so this pruning can never touch
operator-curated entries.

The deployment's static `hermes-code` baseline includes MiniMax M-series
aliases plus Synthetic `syn:large:text`, `syn:small:text`,
`syn:large:vision`, and `syn:small:vision`, plus Groq's current GPT-OSS 20B,
GPT-OSS 120B, and Qwen 3.6 27B routes, plus Arcee `trinity-mini`.
MiniMax M2.x models are text-only;
the current MiniMax M3 API supports image input and tool use. The Synthetic
vision aliases, Xiaomi `mimo-v2.5`, and MiniMax M3 provide multimodal capacity.

## Modality evidence

Tool-call qualification and modality qualification are separate. The gateway
stores modality evidence in the persistent SQLite database configured by
`TUSKER_MODEL_CAPABILITY_DB_PATH` (default:
`model_capability.db` beside `model_quality.db`). Records are keyed by
`provider/model/capability`, for example `input_image`, `output_image`, and
`image_generations`, and contain only status, evidence source, HTTP status,
latency, failure class, probe version, and timestamp. Response bodies and
credentials are never stored.

Catalog metadata is recorded as `advertised`; the existing media capability
registry is recorded as `discovered`; an explicit live test is recorded as
`passed`, `unsupported`, or `unavailable`. A live `unsupported` result can
exclude that modality from pool selection, while `unavailable` remains
retryable and does not permanently remove a model after a quota, auth, or
transport incident.

For non-text requests, missing evidence is treated as ineligible rather than
silently sending an image/audio/video payload to an unknown model. A catalog
claim or a successful modality probe is required; text-only requests retain
the legacy metadata-free behavior. This keeps image recognition fail-closed
while providers refresh incomplete catalogs.

Run bounded input tests from the gateway build host or pod:

```bash
tusker-gateway-qualify-modalities --all-pools --input-modality image
tusker-gateway-qualify-modalities --provider synthetic --include-unadvertised
```

Image, audio, and video generation are not probed automatically by the
modality runner. Their endpoints may be billable or create asynchronous jobs,
so those tests require a provider-specific, explicitly approved probe before
they should be added. The pre-existing Z.AI media discovery is also disabled
unless `TUSKER_CAPABILITY_PROBE_GENERATION=true` is explicitly set. Use
`/status` to inspect the aggregate `model_capabilities` counts and each pool
candidate's stored records.


## Migration history

- **2026-08-21**: Codex OAuth credentials migrated from `hermes.tusker.net.au`
  to `ai.tusker.net.au`. See `docs/migrations/2026-08-21-codex-migration/`.
- **2026-08-21**: Codex request body shape fixed (assistant role format,
  reasoning parameter, codex 400 body logging).
- **2026-08-21**: Heavyweight slug override + per-pool tier gate added
  (`tusker_gateway/heavyweight.py`, mirror of hermes-agent's
  `_HERMES_HEAVY_MODEL_OVERRIDES`).
- **2026-09-10**: Ollama Cloud pricing overlay (`OLLAMA_CLOUD_PRICING`)
  added — models.dev has no ollama-cloud costs, so `glm-5.1`/`glm-5.2`/
  `glm-5.3`/`kimi-k3` previously passed the cheap-pool heavyweight gate
  and burned session quota (`kimi-k3` also added to
  `HEAVYWEIGHT_SLUG_OVERRIDES` for static pool lists).

- **2026-09-19**: Global pricing threshold lowered from `$8/M` to `$3/M`
  output (input remains `$1/M`). Added static slug overrides for
  `kimi-k2.6` and `kimi-k2.7-code`; code/privacy pools now classify all
  catalog models at or above either boundary as heavyweight.
