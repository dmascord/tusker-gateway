# Gateway model routing

How the gateway picks a (provider, model) pair when a client sends a virtual
alias like `hermes-code`, `hermes-privacy`, `hermes-premium`, or `hermes-swarm`.

## Pool tiers

The gateway has four pools, each with a different tier of models:

| Pool | Tier | Heavyweight allowed? | ZDR enforced? |
|---|---|---|---|
| `code` | Cheap (free / OpenRouter free tier / opencode-zen free) | No (dropped) | No |
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
   - Cooldown active for this (provider, model)
   - ZDR + EXCLUDED_PROVIDERS env var (privacy pool only)
3. **Rank by quality score** (descending) from `model_quality.db`. New models
   use an adaptive floor (median - 20.0 clamped to 20.0).
4. **Pick the top-ranked candidate** and remember it for the session.

## Dynamic catalog refresh (planned)

The gateway will pull live model catalogs from upstream providers at runtime
to surface new free-tier models without requiring a config edit:

| Provider | Endpoint | TTL |
|---|---|---|
| Codex | `https://chatgpt.com/backend-api/codex/models?client_version=0.0.0` | 60 min |
| GitHub Copilot | `https://api.githubcopilot.com/models` | 5 min |
| OpenRouter | `https://openrouter.ai/api/v1/models` | 60 min |
| models.dev | `https://models.dev/api.json` (pricing DB) | 60 min |

The static `TUSKER_POOL_*` configs serve as the explicit **allowlist** — a model
must be listed there to be eligible. The dynamic catalog then **extends** the
list with new slugs as upstream providers ship them. To add a brand-new model
to the pool, add it to `TUSKER_POOL_*`; the catalog refresh will keep it
present even if upstream renames or moves the slug.

This is the feature being implemented alongside the heavyweight gate.

## Migration history

- **2026-08-21**: Codex OAuth credentials migrated from `hermes.tusker.net.au`
  to `ai.tusker.net.au`. See `docs/migrations/2026-08-21-codex-migration/`.
- **2026-08-21**: Codex request body shape fixed (assistant role format,
  reasoning parameter, codex 400 body logging).
- **2026-08-21**: Heavyweight slug override + per-pool tier gate added
  (`tusker_gateway/heavyweight.py`, mirror of hermes-agent's
  `_HERMES_HEAVY_MODEL_OVERRIDES`).
