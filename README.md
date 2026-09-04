# tusker-gateway

Lightweight OpenAI-compatible API gateway. Focused exclusively on the API server — no desktop, no CLI.

## Architecture

```
config.py      — env-var configuration + pool definitions
errors.py      — OpenAI-compatible error types
sse.py         — SSE framing utilities
cooldown.py    — per-(provider, model) cooldown tracking with 429 parsing
auth.py        — Bearer token verification (fail-closed)
health.py      — /health, /ready, /status endpoints
quality.py     — SQLite-backed per-model quality scoring (success rate + latency decay)
semantic_cache.py — Scoped, bounded semantic response cache (opt-in)
pools.py       — PoolManager: candidate lists, selection, session stickiness
routing.py     — role-alias routing + passthrough detection
passthrough.py — provider HTTP client + Codex OAuth token rotation
endpoints.py   — OpenAI chat, responses, rerank, image, TTS, and video handlers
app.py         — aiohttp application factory + entry point
__main__.py    — python -m tusker_gateway
```

## Media endpoints

| Surface | Route | Providers |
|---|---|---|
| Image understanding | `/v1/chat/completions`, `/v1/responses`, `/v1/messages` | Any selected vision-capable chat provider; OpenAI, Anthropic, and Responses content blocks are normalized without downloading remote images in Tusker. |
| Image generation | `/v1/images/generations` | OpenAI/Codex, OpenRouter, Google Gemini/Imagen, Z.AI CogView/GLM-Image. |
| Image edits/variations | `/v1/images/edits`, `/v1/images/variations` | OpenAI-compatible image providers; unsupported provider surfaces fail explicitly. |
| Video generation | `/v1/videos` | OpenAI Sora, OpenRouter video models, Google Veo, Z.AI CogVideoX/Vidu. `wait=false` returns the upstream job; waited Z.AI results retain the signed result URL. |
| Reranking | `/v1/rerank` | Cohere v2, Voyage, and Jina native rerank APIs with provider fallback. `hermes-reranker` is the virtual model alias. |

Capability discovery refreshes supported image/video models from provider catalogs. Anthropic supports image input for understanding, not image generation. Media and rerank routes use the same authenticated per-key rate limit, budget, and guardrail preflight as chat.

## Virtual model aliases

| Alias | Pool | Purpose |
|---|---|---|
| `hermes-code` | `code` | General coding models (gpt-5.5, claude-sonnet-4.6) |
| `hermes-privacy` | `privacy` | ZDR-only lightweight models (gpt-5.6-luna, gpt-5.4-mini) |
| `hermes-premium` | `premium` | Heavyweight models (gpt-5.6-sol, gpt-5.6-terra) |
| `hermes-swarm` | `swarm` | Multi-model swarm routing |

## Key design decisions (from production learnings)

1. **Session stickiness** — once a (provider, model) is selected for a session, reuse it until context window is exceeded or model becomes unavailable.
2. **Quality scoring** — `success_rate * 80 + latency_bonus * 20` with exponential decay. Stored in SQLite, shared across restarts.
3. **Cooldown tracking** — per-(provider, model) with 429 body parsing. Weekly limit → 7-day cooldown, hourly → 1h, capped at 1h max.
4. **Heavyweight filtering** — ZDR/privacy pools exclude heavyweight models by default.
5. **Codex token rotation** — shared OAuth pools select credentials round-robin; failed requests continue with the next scheduled credential.
6. **Virtual alias guard** — the advertised model name (e.g. `tusker-gateway`) is never persisted as a session's model or sent to a provider.
7. **Semantic cache isolation** — approximate response reuse is restricted to deterministic text requests and scoped by caller, pool, and concrete route; tool calls and ZDR traffic are excluded. See `docs/semantic-cache.md`.
