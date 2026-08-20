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
pools.py       — PoolManager: candidate lists, selection, session stickiness
routing.py     — role-alias routing + passthrough detection
passthrough.py — provider HTTP client + Codex OAuth token rotation
endpoints.py   — /v1/models, /v1/chat/completions handlers
app.py         — aiohttp application factory + entry point
__main__.py    — python -m tusker_gateway
```

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
5. **Codex token rotation** — pool of OAuth credentials, rotated on failures.
6. **Virtual alias guard** — the advertised model name (e.g. `tusker-gateway`) is never persisted as a session's model or sent to a provider.
