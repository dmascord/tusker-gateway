# Enterprise controls

Tusker's enterprise controls layer over the existing `API_KEYS`
authentication without exposing raw credentials in policy, logs, traces, or
state databases. Identity policy, audit persistence, and idempotency remain
opt-in so an existing deployment can roll forward without a flag-day migration.
Request deadlines default to the gateway's existing 120-second upstream limit.

## 1. Tenant identities and least privilege

`TUSKER_IDENTITIES_JSON` maps a SHA-256 API-key fingerprint to a principal,
tenant, and optional allowlists:

```json
{
  "<64-character-sha256>": {
    "principal": "svc-build",
    "tenant": "engineering",
    "scopes": ["inference:chat", "models:read"],
    "allowed_pools": ["code", "privacy"],
    "allowed_models": ["hermes-code", "openrouter/*"],
    "allowed_providers": ["openrouter"]
  }
}
```

Generate a fingerprint without putting the key in shell history:

```bash
python -c 'import getpass,hashlib; print(hashlib.sha256(getpass.getpass("API key: ").encode()).hexdigest())'
```

Available scopes are:

| Scope | Routes |
|---|---|
| `inference:chat` | Chat Completions, Responses, Anthropic Messages |
| `inference:images` | Image generations, edits, variations |
| `inference:audio` | Text-to-speech |
| `inference:video` | Video generation |
| `inference:rerank` | Reranking |
| `models:read` | Model catalog |
| `status:read` | Detailed runtime status |
| `admin:read` | Read-only admin API (`/admin/*`) |
| `*` | Every capability |

Allowlist entries use shell-style patterns. Omitted lists default to `*`; an
explicit empty list denies that dimension. Media requests use the logical
`media` pool and reranking requests use `rerank` for pool allowlists. Set
`TUSKER_IDENTITY_REQUIRED=true` only after every accepted key has a profile.
Pool aliases and concrete routes are both enforced: every configured fallback
pool must be allowed, and the selected model must match its bare model ID,
`provider/model`, or `provider::model`. When restricting a virtual alias, list
both the permitted alias and the permitted concrete-model patterns.
In strict mode, startup fails if the identity JSON is absent or malformed, and
a valid key without a profile is denied with HTTP 403. Access logs include
principal, tenant, and the key fingerprint. Anthropic `x-api-key` requests use
the same identity and quota path.

## 2. Integrity-chained audit log

Set `TUSKER_AUDIT_LOG_PATH=/home/tusker/.hermes/audit.jsonl` to write one
bounded metadata event per API request. Events include identity, route outcome,
request ID, selected provider/model/pool/cache state, status, and latency. They
never include headers, prompts, completions, tool arguments, or raw API keys.

Each record contains the previous record's digest and its own digest. Set
`TUSKER_AUDIT_HMAC_KEY` from a Kubernetes secret to make the chain
tamper-evident to parties that can edit the file but do not hold the integrity
key. Without the key, the chain uses plain SHA-256 and detects accidental
damage only. File appends use an exclusive lock, mode `0600`, flush, and fsync.
Caller-derived metadata is bounded before serialization so an oversized model
or routing label cannot make the next chain append unreadable.

High-impact tool approvals use the same chain. When native OMP approval is
enabled, the gateway records `tool.approval.proposed` and then
`tool.approval.decision` events for `accepted`, `denied`, or `expired`
decisions. These contain the request correlation ID, provider/model, action
category, tool names, and a hash of the exact normalized call. They do not
contain raw tool arguments, prompts, secrets, or file contents. The decision
event also records `execution_result=not_observed`: the gateway can observe
the approval, but the client executes the tool and must provide any later
result separately.

This provides an offline dataset for measuring approval rates, cancellations,
decision latency, and outcomes by risk category without turning acceptance
behavior into automatic authorization. Keep the audit file on immutable or
access-controlled storage and apply the same retention policy as other
security records.

For temporary investigation, set `TUSKER_HIGH_IMPACT_MODE=audit`. This keeps
the classifier active but records `high_impact.audit` events and allows the
request to continue without emitting an interactive OMP question. Events
record the trigger category, provider/model, request ID, source role and
message index, a hash of source user content, the matched policy phrase when
available, tool names, and a hash/signature of the normalized tool call. Raw
prompts, tool arguments, and secrets are not recorded. The default is
`approval`; return to that mode after the investigation. Audit persistence
continues to follow `TUSKER_AUDIT_FAIL_CLOSED`.

Operational knobs:

- `TUSKER_AUDIT_FAIL_CLOSED=true` rejects unprepared responses if audit
  persistence fails. The default is fail-open with an error log.
- `TUSKER_AUDIT_FSYNC=false` trades durability for write latency.
- `TUSKER_AUDIT_EXCLUDE_PATHS` defaults to `/health,/ready,/metrics`.

Use external log rotation or shipping with copy-truncate disabled. Renaming the
active file is safe; the next file starts a new chain and should be retained
with the prior segment and its final hash.

## 3. Request deadlines

Every `/v1/*` request has a 120-second end-to-end deadline by default. Override
the deployment defaults with:

- `TUSKER_REQUEST_TIMEOUT_MS` — default request deadline; `0` disables it.
- `TUSKER_MAX_REQUEST_TIMEOUT_MS` — hard cap for client overrides.
- `TUSKER_ALLOW_CLIENT_TIMEOUT=false` — ignore client overrides.

Clients may request a shorter or longer bounded deadline with
`X-Tusker-Timeout-Ms`. Non-streaming expiry returns an OpenAI-compatible HTTP
504 with code `request_timeout`. If an SSE response is already prepared, the
gateway cancels provider work and closes the stream; it cannot safely send a
second HTTP status line.

## 4. Persistent idempotency

Enable duplicate suppression with `TUSKER_IDEMPOTENCY_ENABLED=true`. A caller
can then send `Idempotency-Key` on any non-streaming `/v1/*` POST. The key is
scoped by API-key fingerprint, method, and path. The database transaction reserves
the operation before provider dispatch, so concurrent duplicates return 409;
a completed 2xx response is replayed with `Idempotency-Replayed: true`. Reusing
the key for a different canonical request returns `idempotency_conflict`.

Streaming and non-JSON (including multipart image edit) requests are never
cached or replayed. Error responses and responses larger than the configured
cap release their reservation.

- `TUSKER_IDEMPOTENCY_PATH` defaults beside the gateway's persistent state.
- `TUSKER_IDEMPOTENCY_TTL_SECS` defaults to 24 hours.
- `TUSKER_IDEMPOTENCY_LOCK_SECS` defaults to 5 minutes.
- `TUSKER_IDEMPOTENCY_MAX_RESPONSE_BYTES` defaults to 2 MiB.

In production, this store resolves through `TUSKER_STATE_DATABASE_URL` to
PostgreSQL, where the reservation transaction is coordinated across gateway
pods. SQLite with `BEGIN IMMEDIATE`, WAL, and a busy timeout remains the local
development/test fallback only; it must not be used on NFS for sustained
multi-replica operation.
Canonical request identity includes normalized query keys and values, so query
ordering does not affect replay while a changed query conflicts. A cancelled or
timed-out operation releases its processing reservation before propagating the
cancellation.

## 4a. Read-only admin API

`tusker_gateway/admin.py` serves an authenticated, read-only JSON view of the
running gateway under `/admin/*`:

| Endpoint | Content |
|---|---|
| `GET /admin/diagnostics` | Aggregated subsystem snapshot (pools, catalog, quality, usage, cooldowns, breakers, state store) |
| `GET /admin/providers` | Provider registry: base URLs, auth kind, whether a key is configured + key fingerprint (never the key) |
| `GET /admin/pools` | Live pool model lists with validity and unkeyed/catalog-unavailable diagnostics |
| `GET /admin/catalog` | Per-provider catalog refresh state (status, entry count, staleness) |
| `GET /admin/cooldowns` | Active model/provider cooldowns |
| `GET /admin/breakers` | Circuit-breaker states |
| `GET /admin/keys` | API-key fingerprints + identity principals/allowlists (never raw keys) |
| `GET /admin/usage` | Provider usage counters, capacity state, rate-limiter buckets |

Authentication matches `/status`: a valid `Authorization: Bearer <key>`.
Identity-profiled callers additionally need the `admin:read` scope; legacy
keys (no profile) get access as with every other unscoped route. The routes
are read-only — there is deliberately no write path here yet; key rotation
still happens through the `tusker-env-vault` secret plus a rollout restart.

## 5. Automated quality and dependency gates

`.github/workflows/ci.yml` runs Python 3.11 and 3.12 compilation, undefined-name
checks, and the deterministic test suite on pushes and pull requests. A separate
least-privilege job audits installed runtime dependencies. Dependabot checks pip
and GitHub Actions dependencies weekly. Live provider tests remain outside CI
because they consume credentials and quota; run them as a controlled deployment
smoke test.

## 6. Client-IP attribution and guardrails

### Client-IP trust gate

`CF-Connecting-IP` and `X-Forwarded-For` are honoured only when the direct
peer (the connection source) belongs to one of the CIDRs in
`TUSKER_TRUSTED_PROXY_RANGES`. The default list covers the Cloudflare IPv4
and IPv6 ranges; override with your own edge / proxy CIDRs when fronting the
gateway with a different network. Requests from untrusted peers are logged
and audited under the raw peer address, never under the spoofable header
value. This gate protects the access log, the audit chain, and rate-limit
identity from impersonation.

### Guardrails

Set `TUSKER_GUARDRAILS_ENABLED=true` to run a preflight pipeline before
provider dispatch:

- `OutputLengthGuard` clamps `max_tokens` to `TUSKER_MAX_OUTPUT_TOKENS`
  (default 4096) so a request cannot exceed the configured output ceiling.
- `PIIRedactionGuard` replaces emails with `[REDACTED-EMAIL]` and
  Luhn-valid card numbers with `[REDACTED-CC]` in user messages only.
  Long digit runs that do not pass the Luhn checksum (e.g. order IDs) are
  passed through unchanged.
- `PromptInjectionGuard` blocks suspicious directives that appear at the
  start of a user message (`^ignore previous instructions`, `^you are now`,
  etc., with an optional `please`/`kindly` politeness prefix). Operator-
  supplied extra patterns from `TUSKER_GUARDRAILS_INJECTION_PATTERNS` match
  anywhere in the message as plain substrings. Assistant and tool messages
  are never scanned.
- `HarnessSystemPromptGuard` prepends an immutable system prompt to
  requests whose principal matches `TUSKER_GUARDRAILS_HARNESS_PRINCIPALS`
  (default `omp-harness`), establishing the untrusted-data delimiter
  contract.


## Recommended rollout

1. Deploy deadlines and observe timeout rates.
2. Add identity profiles while strict mode is off; verify principal/tenant in
   access logs and `/status`.
3. Enable strict identity mode.
4. Enable idempotency on the persistent volume for retrying clients.
5. Enable HMAC audit output, ship it to immutable storage, then decide whether
   fail-closed behavior matches the service's availability requirements.
