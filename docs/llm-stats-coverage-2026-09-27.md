# LLM Stats ranking coverage investigation

Date: 2026-09-27

## Finding

The public rankings endpoint used by the gateway,
`https://api.llm-stats.com/stats/v1/rankings`, returns a maximum of 50
models. `limit > 50` returns HTTP 422. `offset`, `page`, `skip`, `cursor`,
`start`, `from`, `per_page`, and other tested pagination parameters do not
move the returned window.

The website homepage embeds deeper rankings in its server-rendered HTML.
The captured page contains 46 ranked arrays, with the largest reaching rank
374/375. Five arrays match the gateway's tracked category rankings at the
API window boundary:

| Gateway category | Matching site array | Site depth |
|---|---:|---:|
| `code` | 35 | 272 |
| `general` | 27 | 374 |
| `reasoning` | 12 | 368 |
| `tool_calling` | 42 | 200 |
| `agents` | 31 | 190 |

The first 50 ordered model IDs match exactly for `general`, `reasoning`,
`tool_calling`, and `agents`; `code` matches 48/50 in the captured snapshots.
This is strong evidence that the website exposes the same rankings beyond the
public API's 50-model cap.

Examples from the deeper site arrays:

- `gpt-oss-20b`: ranks 104–228 across captured category arrays.
- `gpt-oss-120b`: ranks 58–172 across captured category arrays.
- `mimo-v2.5`: ranks 45–135 across captured category arrays.

Under the gateway cutoff of 25, these models would be explicitly excluded
where a matching category array contains them. The current API-only sync
records them as `unknown` because they are outside every returned top-50
window.

## Detail endpoint

`GET /stats/v1/models/{id}` exposes benchmark records. Its `scores[].rank`
values are positions within individual benchmarks, not aggregate category
positions. For example, `gpt-oss-20b` has MMLU rank 41 while absent from the
aggregate general top-50 window. Those values must not be compared with the
aggregate category cutoff.

## Production decision
## Implementation (option 3: website seed import)

Option 3 is now implemented as an explicit operator tool. The gateway no
longer defaults to API-only for out-of-window models; instead:

- Models outside every API top-50 window can be seeded from the llm-stats.com
  homepage, which exposes the same rankings to depth ~374 under named
  `category_id` keys (`agents`, `code`, `general`, `reasoning`,
  `tool_calling`).
- Seeds are written with `evidence=site_seed` and age out after
  `TUSKER_LLM_STATS_SEED_MAX_AGE_SECS` (default 7 days), degrading back
  to `unknown` unless refreshed by re-running the importer.
- API evidence always wins: on the next daily sync a seed row for a model
  that appears in any category window is overwritten with
  `category_window` evidence.
- The site arrays are anchored against the API windows via strict ordered
  prefix equality (>90% positional overlap when the key matches). This
  self-calibrating match prevents wrong verdicts if the site changes shape.

See ``docs/gateway-model-routing.md`` ("Website seed provenance") for the
operator workflow and full mechanics.

## Production decision (historical context)

Previously considered options before implementation:

1. Ask the provider to expose cursor/offset pagination for
   `/stats/v1/rankings` or document the internal full-depth endpoint.
2. Keep the current API-only behavior: models outside every top-50 window
   remain `unknown`, selectable, and receive no rank bonus.
3. If website ingestion is required, add it only as an explicit,
   opt-in, best-effort source with prefix matching against the authoritative
   API window and automatic fail-open fallback. It must not replace API
   evidence silently.
