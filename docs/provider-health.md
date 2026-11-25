# Provider health: reporting pool state to clients

## The problem

The gateway knows which provider pools are unhealthy: which credentials are
cooling down, which capacity groups are saturated, and how many requests
actually succeeded today. A client cannot derive any of that from a chat
response — a 200 looks identical whether the winning route was the only healthy
one or one of thirty.

Before this endpoint, reading that state meant shelling into the cluster and
calling `/status` by hand.

## The endpoint

`GET /v1/provider-health`, authenticated by the gateway's normal Bearer
middleware.

```json
{
  "generated_at": 1795580955,
  "summary": {"providers": 2, "ready": 2, "degraded": 0},
  "providers": [
    {
      "provider": "groq",
      "ready": true,
      "cooling_routes": 0,
      "next_reset_seconds": 0,
      "requests_today": 8,
      "failures_today": 0,
      "success_rate_today": 1.0
    }
  ]
}
```

`capacity` is included per provider when a capacity group has counters:

```json
"capacity": {"active": 1, "limit": 2, "queued": 3, "rejections": 0}
```

Nothing secret is exposed: no credential material, no upstream URLs, no
request or response bodies. `tests/test_provider_health.py` asserts that.

## What `used_percent` does *not* mean

This is a **router** view, not a vendor-quota view.

`x-codex-primary-used-percent` from OpenAI describes one ChatGPT seat. The
gateway pools several Codex credentials and fronts seven providers. Reporting
one seat's quota as the pool's number would be wrong exactly when the pool is
unhealthy — the situation the number exists to describe.

So the semantics here are "share of the pool currently unavailable", derived
from real cooldown state. A credential parked on a 15-day cooldown counts as
out of service, not as 63% consumed.

## Rendering it inside OMP

OMP will not fetch this on its own. It classifies providers by URL marker and
rewrites any non-ChatGPT `baseUrl` to `chatgpt.com` before a usage fetch, so
impersonating `openai-codex` is neither possible nor safe.

What does work is OMP's own extension API, which is configuration rather than a
code change. `~/.omp/agent/extensions/gateway-health.ts` registers a
`UsageProvider` for the gateway and maps the payload onto usage rows. OMP then
caches, stores and renders it like any built-in provider's usage.

Verified output against a degraded fixture:

```text
● Providers degraded               ███████████████████░░░░░░░░░  66.7% used
● openai-codex cooling (cooldown)  ████████████████████████████  100.0% used · resets in 1m58s
● alibaba cooling (cooldown)       ████████████████████████████  100.0% used · resets in 14m58s
```

Against the live endpoint with everything healthy:

```text
● Providers degraded  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░  0.0% used
```

### Usage-row contract notes

Discovered by instrumentation, not guesswork:

- `fetchUsage(params, ctx)` receives
  `{provider, credential, baseUrl, credentialId, accountKey, signal}`.
- `credential.apiKey` is the field the host populates; OMP's `auth import`
  stores `key`, so a credential imported via CLI will not reach a custom
  `fetchUsage` unless it also carries `apiKey`.
- `amount.unit` must be one of `tokens | requests | credits | minutes | bytes |
  percent | usd` — anything else renders a literal `undefined` in the label.
  Omit the unit or use `"unknown"` when the value is a plain count.
- `amount.usedFraction` drives the bar; `used`/`limit` only supply the text.
- A provider id already present in `models.json` does **not** receive the
  extension's `baseUrl`; register the provider from the extension itself, or
  read the URL from an environment variable.

## Verification

- `tests/test_provider_health.py` — 7 tests, covering the ready/degraded
  branches, distinct-cooldown counting, capacity inclusion, the
  no-secret-leak assertion, and unknown-vs-zero success rate.
- Live route: 401 without a Bearer token, 200 with real data.
- End-to-end: `omp usage --provider hermes-gateway` renders gateway state.
