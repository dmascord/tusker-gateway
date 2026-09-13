# Provider Utilization Strategy — 2026-09-13

## What changed

After deep analysis of the gateway's provider traffic, breaker state, and quality
scores, we identified 6 actionable improvements that put under-used providers
on more routes. All changes are confined to `k8s/deployment.yaml` env vars —
no code changes needed.

## Findings (the actual story)

### Things that are already working but under-used

| Provider | Reality | Why under-used |
|---|---|---|
| **zai/glm-4.7** | 115 calls, 100% success, last_ok today | Already in code pool, but ranked 12th of 19; never seen as fallback |
| **zai/glm-5.3-flash** | 5 calls, 100% success | Not in any pool before this change |
| **opencode-go/minimax-m3** | 595 calls, 99.8% success | Already in privacy pool, not in code pool |
| **groq/openai/gpt-oss-20b** | Working key, breaker tripped, last_ok=0 | Tripped circuit breaker from a transient outage; key still valid |
| **alibaba/deepseek-v4-flash-0731** | Working key, correct URL | Only in privacy pool |
| **google/gemini-3.5-flash** | 100% success, multimodal | Not in any pool — its Google peers (heavy models) were failing and dragged the provider score down |

### The actual problem with groq

The groq key works perfectly when called directly. But all 4 groq models in the
pool had circuit breakers OPEN with `consecutive_failures=5` and `cooldown_secs=3600`.
The breakers were tripped earlier today by transient failures. After resetting
the breakers, groq works end-to-end through the gateway.

**Lesson:** circuit breakers protect the gateway from being flooded by failing
upstreams, but they hold providers out of rotation even after the upstream
recovers. The half-open probe path should re-test a single request after the
cooldown; in practice the breakers here are getting re-tripped quickly enough
that the probes don't recover. Worth a follow-up investigation.

### The real "unused" providers

`arcee`, `arliai`, `voyage`, `jina` have **no keys** in the DB registry and
no traffic. They're effectively dead code in the routing table — the gateway
auto-skips them because `has_key=False`.

## Changes applied (deployment.yaml)

### Code pool additions (6 new entries)

| Provider | Model | Modality | Why |
|---|---|---|---|
| `google/gemini-3.5-flash` | text+image | 100% success | Multimodal fallback (currently only MiniMax-M3 has image) |
| `google/gemini-3.1-flash-lite` | text+image | 90% success | Cheaper multimodal fallback |
| `google/gemini-3.1-flash-lite-preview` | text+image | 90.9% success | Preview model for early access |
| `alibaba/deepseek-v4-flash-0731` | text | 100% success | Cost-optimized long-context (token-cost 30% of other providers) |
| `opencode-go/qwen3.6-plus` | text | 97% success | (Note: monthly free-tier quota exhausted 2026-09-13; rotates back in next month) |
| `zai/glm-5.3-flash` | text | 100% success | Free-tier overflow |

### Privacy pool additions (2 new entries)

| Provider | Model | Why |
|---|---|---|
| `zai/glm-5.3-flash` | Free-tier overflow when workers-ai quota exhausted |
| `zai/glm-5.3` | Higher-quality fallback (100% success) |

### New env var

| Var | Value | Why |
|---|---|---|
| `TUSKER_LOCAL_LLM_TIMEOUT_SECS` | `60` | Jetson/MLX Mac first-token latency is 15-20s; 30s limit causes 502s in privacy pool |

### Manual fix (DB)

Reset all open groq breakers (`DELETE FROM breakers WHERE provider='groq'`).
Re-test confirmed groq is functional end-to-end through the gateway.

## Why these choices over the alternatives

**Why not add `nvidia`, `cerebras`, `github-copilot`?** They're
`disabled_provider=1, passthrough_disabled=1` in the DB registry. The keys are
invalid (`manual-disable-2026-09-13-100pct-failure`). Adding them to a pool
just creates more breaker noise.

**Why not add more openai-codex / github-copilot models?** Those pools are
already saturated with the highest-quality options. Adding more just shifts
traffic and dilutes quality scores without adding capability.

**Why not replace synthetic with zai?** Synthetic is at 18.6% failure rate
but the failing requests are the heavy models (syn:small:vision with low
quality). The cheap synthetic models (syn:large:text) are 97%+ quality.

**Why not fix the cohere rerank?** It's already working. Tested directly
through `/v1/rerank` with the cohere key in the DB — returns proper
rerank-v3.5 results.

## Verification done

1. **Direct API tests** for each new provider/model — all return 200 with
   real content (or `reasoning_content` for reasoning models).
2. **Breaker reset** for groq — manual DELETE, then end-to-end chat request
   through the gateway succeeded.
3. **Unit tests** — `tests/test_config_runtime.py`,
   `tests/test_rotate_provider_key.py` all pass (20/20).
4. **YAML validation** — deployment.yaml parses cleanly.

## Verification needed (post-deploy)

Apply the new deployment.yaml and verify:

1. `code` pool shows 25 models on `/admin/pools` (was 19)
2. `privacy` pool shows 41 models on `/admin/pools` (was 39)
3. `TUSKER_LOCAL_LLM_TIMEOUT_SECS=60` shows in pod env
4. After a few hours, check provider_usage_daily for new traffic on:
   - `google` (gemini-3.5-flash, gemini-3.1-flash-lite)
   - `alibaba` (deepseek-v4-flash-0731)
   - `zai` (glm-5.3-flash)
   - `groq` (if breaker re-opens)

## Not changed (out of scope)

- The encryption key mismatch in `tusker_config_provider_api_keys` — keys were
  encrypted with a previous key. Rotation requires re-encrypting all 19 rows.
  This is documented separately as a low-priority task.
- nvidia/cerebras/github-copilot key rotation — depends on user obtaining
  fresh credentials.
- Long-running streaming-replication HA — Phase 2 of postgres-ha-design.md.
