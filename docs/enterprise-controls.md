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
scoped by API-key fingerprint, method, and path. The SQLite transaction reserves
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

The database uses `BEGIN IMMEDIATE`, WAL, and a busy timeout for multi-process
safety on a single host. Do not place SQLite on NFS for sustained multi-replica
operation; move this store and the existing quota state to a transactional
shared database before running active-active replicas.
Canonical request identity includes normalized query keys and values, so query
ordering does not affect replay while a changed query conflicts. A cancelled or
timed-out operation releases its processing reservation before propagating the
cancellation.

## 5. Automated quality and dependency gates

`.github/workflows/ci.yml` runs Python 3.11 and 3.12 compilation, undefined-name
checks, and the deterministic test suite on pushes and pull requests. A separate
least-privilege job audits installed runtime dependencies. Dependabot checks pip
and GitHub Actions dependencies weekly. Live provider tests remain outside CI
because they consume credentials and quota; run them as a controlled deployment
smoke test.

## Recommended rollout

1. Deploy deadlines and observe timeout rates.
2. Add identity profiles while strict mode is off; verify principal/tenant in
   access logs and `/status`.
3. Enable strict identity mode.
4. Enable idempotency on the persistent volume for retrying clients.
5. Enable HMAC audit output, ship it to immutable storage, then decide whether
   fail-closed behavior matches the service's availability requirements.
