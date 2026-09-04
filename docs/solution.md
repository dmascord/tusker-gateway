# Tusker Gateway Solution

## Purpose

`tusker-gateway` is a self-hosted OpenAI-compatible gateway for routing model
requests across multiple hosted and local providers. It is optimized for
Hermes deployments where provider credentials, privacy constraints, quality,
and fallback behavior must be controlled centrally.

## Request flow

```text
OpenAI-compatible client
        |
        v
 aiohttp application
        |
 authentication middleware
        |
 endpoint handler (/v1/chat/completions, /v1/responses)
        |
 PoolManager: pool -> candidate model/provider
        |
 PassthroughClient
   +----+------------------+
   | auth strategy         |
   | bearer / OAuth        |
   +----+------------------+
        |
 provider API / chained gateway
```

The gateway preserves OpenAI-style request and response contracts while
isolating provider-specific URLs, headers, token exchange, and failure modes.

## Application and endpoints

`app.py` creates the `aiohttp.web.Application`, loads configuration at startup,
creates the shared HTTP session, installs authentication middleware, and
registers:

- `GET /health`: liveness response.
- `GET /ready`: readiness response; currently requires at least one credential.
- `GET /status`: pool and quality status.
- `GET /metrics`: Prometheus text exposition (Release 1; optional auth via
  `TUSKER_METRICS_TOKEN`).
- `GET /dashboard` + `/dashboard/partials/*`: server-rendered status dashboard
  with HTMX auto-refresh (Release 2).
- `GET /v1/models`: exposed model catalog, including the `hermes-reranker` alias.
- `POST /v1/chat/completions`: OpenAI chat completion proxy.
- `POST /v1/responses`: Responses-compatible endpoint.
- `POST /v1/rerank`: provider-aware reranking proxy with Cohere-compatible results.

The shared `aiohttp.ClientSession` avoids per-request connection setup. Cleanup
closes it during application shutdown.

## Provider configuration

`PROVIDER_ENDPOINTS` maps provider identifiers to:

- `base_url`
- `chat_path`
- optional native `rerank_path` for dedicated reranking providers
- `auth_type`: `bearer` or `oauth`
- optional provider model header

`ProviderConfig` in `models.py` provides a typed representation and preserves
the existing raw configuration shape at module boundaries.

The gateway supports provider API keys from configuration/environment and
provider-specific OAuth credentials. Unknown providers fail explicitly rather
than silently routing to a default endpoint.

## Pool routing

`PoolManager` builds virtual role pools from configuration. A pool contains
(provider, model) candidates with context-window and policy metadata:

- `hermes-code`: normal coding rotation.
- `hermes-privacy`: ZDR-compatible candidates.
- `hermes-premium`: heavyweight or premium candidates.
- `hermes-swarm`: swarm-oriented candidates.

Selection considers session stickiness, preferred candidates, context-window
fit, cooldown state, privacy/ZDR policy, heavyweight restrictions, and quality
ranking. Session stickiness lasts one hour and keeps multi-turn conversations
on a stable backend where possible.

`ModelSpec` records `context_window`, `heavyweight`, and `zdr_ok`. Privacy pools
exclude heavyweight models by construction; this is a policy invariant, not a
runtime-only environment convention.

## Authentication and credentials

Authentication is split from request orchestration in `auth_strategies.py`:

- `BearerAuthenticator` selects an explicit or configured provider key.
- `OAuthAuthenticator` obtains a raw token from `CodexTokenRotator`, exchanges
  it for a short-lived provider token, and applies Copilot headers.

`CodexTokenRotator` supports both:

- Hermes format: `access_token`, `refresh_token`, `expires_at_ms`.
- Legacy format: `token`, `refresh_token`, `expires_at`.

The rotator selects credentials round-robin and refreshes near-expiry
credentials; failed requests continue with the next scheduled slot. Updated
credentials are persisted through `save_auth_file` in Hermes-compatible format.
`copilot_enroll.py` implements GitHub device-code enrollment and stores
provider, label, priority, source, refresh metadata, and request metadata.

`Credential` in `models.py` normalizes legacy and Hermes fields while preserving
unknown metadata on serialization. Secrets must never be logged, returned by
health/status endpoints, or committed to source control.

## Copilot exchange

`copilot_exchange.py` handles short-lived API-token exchange and enterprise URL
derivation. It caches exchanged tokens by a fingerprint of raw token and
exchange endpoint, with a refresh margin before expiry. Standard Copilot headers
are generated centrally, including editor version, integration ID, initiator,
and vision-request indicators.

`copilot_constants.py` is the single source for client IDs, exchange URLs,
editor/user-agent values, polling parameters, and refresh margins.

## Quality routing

`QualityDB` stores model call outcomes and latency events in SQLite. Pool
selection uses quality information to prefer successful, responsive candidates.
Quality is best-effort: a database failure must not prevent a provider request.

The observable quality contract is:

- successful calls update model success statistics;
- failed calls update failure statistics;
- latency is recorded as an event;
- quality data influences candidate ordering without bypassing privacy policy.

## Cooldowns and retry windows

429 responses are parsed by `_cooldown_seconds_for_429`. The parser honors
`Retry-After` and recognizes semantic windows such as hourly, daily, and weekly
limits, subject to the configured cap.

Cooldown scope follows provider semantics: OpenRouter, Google Gemini, Groq, and
Anthropic are model/model-class scoped, so a 429 from one model does not suppress
healthy sibling models. Providers without documented model-scoped limits retain
provider-wide cooldowns. An explicit provider-level cooldown (for example, the
failure circuit after repeated transport errors) still blocks the whole provider.

`CooldownTracker` provides fast in-memory filtering. `PersistentCooldownStore`
adds SQLite persistence in `cooldowns.db`, records provider/model windows, and
hydrates active cooldowns into the tracker at application startup. This avoids
immediate retry storms after a pod restart.

Cooldown persistence is best-effort and must not make an otherwise valid request
fail. Expired rows are purged during startup and can be purged explicitly.

## Health and readiness

`/health` is liveness: process and HTTP server are responding.

`/ready` is a lightweight operational gate. It returns HTTP 503 when the local
credential pool is empty; otherwise it returns the credential count. It does not
perform a provider request, so readiness remains deterministic and does not
consume quota. Provider health is exposed through pool/quality status rather
than a readiness request side effect.

## Error behavior

- Unknown provider: explicit provider error.
- Authentication failure: provider auth error.
- Forbidden response: provider access error.
- 429: cooldown calculation, in-memory marking, persistent recording, then
  propagate the rate-limit error to the caller/fallback layer.
- 5xx and transport errors: provider error and quality failure event.
- Streaming responses: response remains open for the async iterator and is
  released when iteration completes or the client disconnects.

## Operational requirements

1. Keep `auth.json`, API keys, and quality/cooldown databases outside images.
2. Mount persistent storage for auth and SQLite state in Kubernetes.
3. Configure a deployment secret or environment for provider keys.
4. Expose `/health` for liveness and `/ready` for readiness. Expose `/metrics`
   for Prometheus scraping (optionally gated by `TUSKER_METRICS_TOKEN`).
5. Use `/status` and quality database inspection for routing diagnosis.
6. Run deterministic tests before deployment and a real provider smoke test
   after rollout.
7. Treat privacy/ZDR policy as a source-code/configuration invariant and review
   every new provider/model entry.

## Release 1 capabilities (opt-in)

All three modules below default to disabled. Enable per-deployment via env:

- `TUSKER_CACHE_ENABLED=true` + `TUSKER_CACHE_PATH` + `TUSKER_CACHE_TTL_SECS`
  + `TUSKER_CACHE_MAX_ENTRIES` to turn on the exact-match response cache.
  Per-request bypass via `X-Tusker-Cache: bypass` header.
- `TUSKER_BUDGETS_ENABLED=true` + `TUSKER_BUDGETS_JSON` (fingerprint-keyed
  caps) + optional `TUSKER_GLOBAL_DAILY_TOKENS` for per-key token budgets.
- `/metrics` is always registered; if `TUSKER_METRICS_TOKEN` is set, callers
  must send `X-Tusker-Metrics-Token: <token>` to scrape.

## Release 2 capabilities (opt-in)

- `TUSKER_CIRCUIT_ENABLED=true` to enable per-(provider, model) circuit
  breakers. Knobs: `TUSKER_CIRCUIT_{CONSECUTIVE,WINDOW,RATIO,COOLDOWN}`.
- `TUSKER_RATELIMIT_ENABLED=true` + `TUSKER_RATELIMIT_JSON` (fingerprint-keyed
  policies, plus optional `default` key) for per-key token-bucket rate limits.
- `TUSKER_OTLP_ENDPOINT=http://collector:4318` to enable OTLP/HTTP-JSON
  tracing export. Optional `TUSKER_OTLP_HEADERS` (JSON dict) for auth headers.
- `/dashboard` is always registered; auth via `TUSKER_METRICS_TOKEN` (same
  as `/metrics`).

## Enterprise controls

- Fingerprint-keyed caller identities add tenant/principal attribution, route
  scopes, and pool/model/provider allowlists without copying raw keys into
  policy configuration.
- Optional JSONL audit events are append-only and SHA-256 or HMAC-SHA-256
  chained. Request content and credentials are excluded.
- End-to-end `/v1` deadlines cancel upstream work and cap client overrides.
- Optional persistent idempotency reserves and replays successful,
  non-streaming POSTs within a caller-specific scope.
- GitHub Actions validates Python 3.11/3.12, deterministic tests, static defect
  checks, and dependency vulnerabilities. See `docs/enterprise-controls.md`.

## Extension rules

For a new provider:

1. Add its endpoint and auth type.
2. Add config/model metadata and tests.
3. Add an auth strategy only if existing bearer/OAuth behavior is insufficient.
4. Add it to explicit pools; do not silently add it to privacy pools.
5. Define provider error and retry semantics.
6. Add deterministic mocked tests and one controlled live smoke test.

Avoid adding provider-specific branches to `PassthroughClient`; extend the
strategy or endpoint abstraction instead.
