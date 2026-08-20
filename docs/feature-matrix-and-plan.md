# Feature Matrix & Roadmap

## Methodology

We surveyed 15+ open-source OpenAI-compatible gateways from the curated
awesome-llm-gateways list, plus commercial aggregators (OpenRouter, Requesty,
Portkey, Helicone, Bifrost). The matrix captures the capabilities most teams
consider when self-hosting an AI gateway.

The matrix uses three signals:

- ✅ = native support, documented or visible in the source repository.
- ⚠️ = partial/limited support; for example, RAM-only state or per-key (not
  per-token) rotation.
- ❌ = not present.

The columns are ordered roughly from "specialized" (Tusker) to "broad"
(LiteLLM/OpenRouter). Rows are grouped by capability class.

## Feature Matrix

| Feature | Tusker | LiteLLM | Portkey | Helicone | Bifrost | OpenRouter | Envoy AI | TensorZero |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Core** |
| OpenAI-format proxy | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Anthropic-format | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| Responses API | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ⚠️ | ✅ |
| **Routing** |
| Pool/role aliasing | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ |
| Session stickiness | ✅ | ✅ | ✅ | ✅ | ⚠️ | ❌ | ⚠️ | ✅ |
| Quality-aware select | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ⚠️ | ✅ |
| Weighted/load-balance | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Auth & credentials** |
| OAuth device-code flow | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Token exchange & refresh | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Multi-credential rotation | ✅ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ❌ | ⚠️ | ⚠️ |
| Hermes auth.json interop | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Reliability** |
| Persistent cooldowns | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Circuit breaker | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Quality DB routing | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ |
| **Observability** |
| Prometheus metrics | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| OpenTelemetry traces | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Dashboard UI | ❌ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ❌ | ✅ |
| **Caching** |
| Exact-match cache | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ |
| Semantic cache | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| **Governance** |
| Per-key rate limits | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Cost budgets | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ |
| Guardrails/PII | ❌ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ |
| **Agentic** |
| MCP proxy | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| A2A gateway | ❌ | ❌ | ⚠️ | ⚠️ | ⚠️ | ❌ | ⚠️ | ❌ |
| Tool routing | ❌ | ⚠️ | ✅ | ⚠️ | ✅ | ❌ | ✅ | ✅ |

## Where Tusker leads

Tusker's distinctive capabilities, none of which LiteLLM/Portkey/Helicone
implement:

1. **OAuth device-code enrollment** — first-class support for Copilot/Codex
   device-code flow, with CLI commands `enroll`, `list`, `import`, `remove`.
2. **Token exchange + persistent rotation** — auto-refresh of Copilot access
   tokens via GHE-derived endpoints, with rotation across a credential pool and
   persistence of the updated pool back to disk.
3. **Hermes `auth.json` compatibility** — reads and writes the live Hermes
   format (`credential_pool.<provider>[]`), enabling direct reuse of credentials
   enrolled for Hermes-agent.
4. **Persistent cooldowns** — cooldown windows survive pod restarts, preventing
   the "restart → immediate 429 → restart" loop that affects in-memory trackers.
5. **Privacy/ZDR policy as code** — heavyweight models are excluded from ZDR
   pools at the model-spec level, not as a runtime convention.

## Where we are behind

The matrix exposes real gaps:

1. **Semantic / exact-match caching** — every other gateway has it; Tusker does
   not. This is the single highest-ROI missing capability.
2. **Cost budgets and per-key limits** — LiteLLM/Portkey set the bar here; teams
   need both per-virtual-key token caps and provider spend caps.
3. **Observability stack** — Prometheus metrics, OpenTelemetry traces, and a
   dashboard. We have status JSON, but no scrape endpoint.
4. **MCP support** — increasingly table stakes for agentic workloads.
5. **Anthropic-format adapter** — Hermes already uses Anthropic, so a
   `/v1/messages` adapter is a quick win.
6. **Circuit breakers** — complementary to cooldowns; trip a provider entirely
   after N consecutive failures.

## Execution Plan (priority-ordered)

The plan is grouped into four releases. Each release is small enough to ship
behind a feature flag and roll back safely.

### Release 1 — Caching & cost basics (target: 2-3 weeks)
- **Exact-match cache** (`/v1/chat/completions`) backed by SQLite.
  - Key: SHA-256 of model + messages + extra_body.
  - TTL: configurable (default 5 min).
  - Bypass header: `X-Tusker-Cache: bypass`.
- **Cost/budget tracking** — `BudgetStore` with daily/weekly caps per virtual
  API key. Return `429` with `X-Tusker-Budget-Reason` when exceeded.
- **Prometheus `/metrics`** — counters for requests, tokens, latency;
  gauges for pool size, cooldown count.

### Release 2 — Observability & governance (target: 3-4 weeks)
- **OpenTelemetry traces** — span for inbound request → auth → pool select →
  provider call; export via OTLP.
- **Circuit breaker** — per-provider rolling window of failures; trip after N
  consecutive or M-of-N failures; half-open probe after cooldown.
- **Per-key rate limits** — token bucket per virtual key, with refill rate.
- **Status dashboard** — minimal `React` or `htmx` page consuming `/status`.

### Release 3 — Semantic caching & intelligence (target: 4-6 weeks)
- **Semantic cache** — embed incoming prompt (local `sentence-transformers`
  MiniLM), compare against cached embeddings via SQLite-VSS or `pgvector`.
- **Guardrails** — PII redaction, prompt-injection detection, output length
  caps; pluggable via a `Guard` interface.
- **Anthropic-format adapter** — `/v1/messages` translates to OpenAI-format
  internally, preserves Claude-specific features where possible.

### Release 4 — Agentic protocol support (target: 6-8 weeks)
- **MCP proxy** — discover MCP servers, expose tools to upstream LLM, mediate
  tool calls.
- **A2A gateway** (lite) — agent-to-agent routing and auth delegation.
- **A/B routing** — split traffic across two providers by hash for comparison.

## Acceptance criteria for the plan

Each release item must:

1. Pass deterministic tests with mocked providers.
2. Pass a real-provider smoke test (OpenRouter, since we already have a key).
3. Roll forward without breaking existing privacy / ZDR invariants.
4. Be opt-in via config so existing deployments are unaffected.
5. Be documented in `docs/solution.md` and reflected in the feature matrix.

## Risks and trade-offs

- **Semantic cache adds an embedding dependency** — keep it optional and
  default to exact-match cache; only enable semantic cache when an embedder
  is configured.
- **Prometheus scrape volume** — counters should be scoped (per-pool), not
  per-request high-cardinality.
- **MCP** — security implications (tool execution); introduce with explicit
  approval flows rather than allow-by-default.
- **Anthropic adapter** — divergent feature surface (extended thinking,
  artifacts); document the unsupported subset rather than feign completeness.

## Conclusion

Tusker has structural advantages in auth/credential lifecycle that no other
gateway replicates. Closing the caching and observability gaps moves it from
"specialized tool" to "general-purpose gateway with best-in-class auth." The
four-release sequence respects existing invariants and ships incremental value
behind feature flags.