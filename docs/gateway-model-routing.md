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

The tier is the source of truth — `PoolManager.pool_keeps_heavyweight()` returns
`True` for `premium`/`swarm` and `False` for `code`/`privacy`. Callers can override
per-request by passing `heavyweight_ok=True/False` to `PoolManager.select()`.

## Heavyweight classification

A model is **heavyweight** when *either*:

1. Its slug is in `tusker_gateway.heavyweight.HEAVYWEIGHT_SLUG_OVERRIDES`, or
2. Its pricing (per 1M tokens) exceeds `cost_input >= $1` OR `cost_output >= $8`
   AND pricing data is available from models.dev.

The slug override set is curated and currently includes:

| Family | Heavy slugs |
|---|---|
| Codex (paid) | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.5`, `gpt-5.4` |
| Anthropic (paid) | `claude-sonnet-4.6`, `claude-opus-4.6`, `claude-sonnet-4.5`, `claude-opus-4.5` |
| Google (paid) | `gemini-2.5-pro`, `gemini-3-pro` |
| Cohere (paid) | `command-a-plus-05-2026`, `command-a-03-2025` |
| Other | `mistral-large-3:675b`, `deepseek-v4-pro` |

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
   - ZDR + EXCLUDED_PROVIDERS env var (privacy pool only)
3. **Rank by quality score** (descending) from `model_quality.db`. New models
   use an adaptive floor (median - 20.0 clamped to 20.0).
4. **Pick the top-ranked candidate** and remember it for the session.

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

Static `TUSKER_POOL_*` entries remain operator-curated baselines. Pools with
`auto_free: true` also receive eligible catalog entries at startup and on each
refresh. Catalog-only entries are pruned when they disappear or become
ineligible; static entries are never pruned.

## Auto-free catalog merge

Pools can opt in to **automatic free-tier discovery** by setting
`auto_free: true` in their `TUSKER_POOL_<name>` JSON env var:

```json
TUSKER_POOL_CODE='{"models": [], "auto_free": true}'
```

Account-backed catalogs can be opted in explicitly with
`auto_catalog_providers`. This is intended for subscription or credited
providers whose catalog does not expose a meaningful zero-price signal:

```json
TUSKER_POOL_CODE='{"models": [], "auto_free": true, "auto_catalog_providers": ["openai-codex", "github-copilot", "github-copilot-enterprise", "opencode-go", "zai", "synthetic"]}'
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

Providers listed in `auto_catalog_providers` use their authenticated catalog
as the account's allowlist rather than the zero-price test. This is the
explicit path for Codex, Copilot, OpenCode Go, Z.AI, Synthetic, MiniMax,
Ollama Cloud, Groq, Google, or Cerebras when their credentials provide model
access.

`TUSKER_AUTO_FREE_EXCLUDED_PROVIDERS` can block a provider from dynamic
catalog discovery while leaving explicitly configured models untouched. The
deployment sets it to `nvidia` while that provider's worker capacity is under
investigation; this prevents a direct NVIDIA `/models` refresh from adding new
routes automatically.

## Provider audit

Run the read-only audit on the cluster build host after a deployment:

```bash
./k8s/audit-provider-pools.sh
POOL=privacy ./k8s/audit-provider-pools.sh
```

It runs the low-concurrency streaming tool qualification against the selected
providers inside the gateway pod and then prints the authenticated catalog
diagnostics from `/status`. Results are written to the gateway's persistent
tool-capability database; response bodies and credentials are not retained.

The privacy pool applies the provider policy before catalog pricing. The
default registry currently allows local `local-llm`, Ollama Cloud, OpenCode
Go, OpenAI Codex, and GitHub Copilot Enterprise. The `local-llm` entry points
at `localhost` inside the gateway pod; it is not the Orin Nano. An Orin route
requires an explicit provider override with the Orin's reachable address.
Public GitHub Copilot,
OpenRouter, NVIDIA trial endpoints, and other direct providers remain outside
the privacy pool unless an explicit deployment policy enables them.

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

**Pruning proof** (`tests/test_catalog.py::test_poolmanager_auto_free_drops_models_that_stop_being_free`):

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
