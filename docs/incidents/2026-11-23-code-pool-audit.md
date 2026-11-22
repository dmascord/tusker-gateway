## 2026-11-23 — Code pool audit (four gates, all 355 routes)

Companion to `2026-11-23-privacy-pool-full-audit.md`. The same four-gate audit
was run against `code` using the deployed image (`1ca806f`) and the corrected
probe settings (16,384-token composite cap, 120s timeout).

## Pool composition and selectability

`code` has **355 routes across 17 providers** — much larger than `privacy`
(200 routes / 8 providers). Heavyweight routes are excluded by design
(`heavyweight=162` in every filter trace).

| Shape | Eligible candidates (fresh process, no live cooldowns) |
|---|---:|
| plain | 193 |
| tools | 83 |
| image | 111 |
| tools+image | **28** (was 25 before this audit) |
| tools+image+212K | **24** (was 21 before this audit) |

The code pool is in substantially better shape than privacy: 24 routes can
serve the hardest shape versus privacy's 8.

## Near-miss groups (single gate missing)

| Gate | Count | Examples |
|---|---:|---|
| `input_image` | 11 | GHE `gpt-5.6-luna`/`gpt-6-luna` (`advertised`), 5 google Gemini models (no record) |
| `input_image_tools` | 21 | alibaba qwen3.6/3.7/3.8-flash (unavailable), google gemini-3.5-flash (unsupported), opencode-cli free models |
| `tool_capability` | 7 | `zai/glm-5.3-flash` (STRUCTURED_STREAM), kilo/openrouter nemotron variants |

## Re-probes performed

- **Modality (`input_image`)**: 5 google models passed — `gemini-2.5-flash`,
  `gemini-3-flash-preview`, `gemini-3.1-flash-lite`, `gemini-3.1-flash-lite-preview`,
  `gemma-4-31b-it`. These had composite and tool evidence already passing, so
  they became immediately selectable: +3 net candidates for tools+image and
  +3 for tools+image+212K. GHE `gpt-5.6-luna`/`gpt-6-luna` returned 503
  `rate_limited` and remain blocked.
- **Composite**: all six near-misses failed — alibaba's three qwen/flash routes
  returned `rate_limited` (account quota), `google/gemini-3.5-flash` returned a
  definitive `non_strict_tool_contract`, and the two opencode-cli free models
  returned `stream_error` / `provider_error`.
- **Tool**: not re-probed; the seven candidates are mostly STRUCTURED_STREAM
  (recoverable via the existing structured-tool recovery probe) or UNAVAILABLE.

## Caveats

The eligible counts above come from a fresh `PoolManager` with a full catalog
refresh and **no live cooldown tracker**. The live app additionally applies
provider/model cooldowns and breakers; earlier `/health` snapshots showed
provider-level cooldowns on alibaba, commandcode, github-copilot-enterprise and
ollama-cloud, so live depth for the hardest shape can be materially lower.

Composite probe flakiness remains the same as in the privacy audit.

No code changes were made; this is a probe/evidence audit only.
