# PostgreSQL shared gateway state

The gateway keeps SQLite as the default for local development and tests. In
the cluster, `TUSKER_STATE_DATABASE_URL` points the shared mutable stores at
the dedicated `tusker-gateway-postgres` service. The stores covered by this
switch are quality, capability evidence, tool qualification, provider usage,
cooldowns, circuit breakers, rate limits, budgets, and idempotency records.

Response caching and the optional semantic cache remain separate because they
are disposable acceleration data. OAuth credentials and provider secrets also
remain in their existing protected stores.

## Bootstrap

Create the password and DSN Secret out of band; neither value belongs in the
repository:

```bash
PASSWORD="$(openssl rand -hex 32)"
kubectl -n hermes create secret generic tusker-gateway-postgres-auth \
  --from-literal=POSTGRES_PASSWORD="$PASSWORD" \
  --from-literal=DATABASE_URL="postgresql://tusker_gateway:${PASSWORD}@tusker-gateway-postgres:5432/tusker_gateway" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Apply `k8s/gateway-postgres.yaml`, wait for its readiness probe, and run the
one-time migration while the gateway has no writer:

```bash
python -m tusker_gateway.tools.migrate_state \
  --dsn "$TUSKER_STATE_DATABASE_URL" \
  --source-dir /home/tusker/.hermes
```

The migration opens SQLite read-only, is safe to rerun, and never removes the
source or forensic copies. The current production capability file is named
`model_capability-recovered.db`; pass `--capability-db` if the source uses a
different name.

Keep the gateway at one replica or use a no-overlap deployment while migrating.
Once the target row counts and gateway smoke tests are verified, rolling
updates can resume because pod-to-pod database writes no longer share SQLite
WAL files on RWX storage.

## PostgreSQL outage behavior

PostgreSQL remains the authoritative shared state backend. The gateway creates
its connection pool lazily and does not block application startup waiting for a
database connection. Connection acquisition is bounded by
`TUSKER_STATE_DATABASE_CONNECT_TIMEOUT`.

During a PostgreSQL outage, advisory state (quality, capability evidence,
provider usage, cooldown persistence, and circuit-breaker persistence) uses
process-local safe defaults and does not write to the old RWX SQLite files.
The existing in-memory cooldown tracker continues to protect the current
process. Rate-limit, budget, and idempotency state are correctness-critical;
their preflight operations return a deliberate `503` until PostgreSQL recovers.
Successful provider responses are not changed into errors if their subsequent
best-effort usage accounting cannot be persisted.

No persistent SQLite fallback is mounted for this mode. A RWO SQLite volume
would prevent multi-writer corruption, but it would still be stale and would
not provide safe cross-replica coordination; it is therefore not used as an
authoritative fallback.
