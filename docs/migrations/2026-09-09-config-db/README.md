# Config DB migration: static env → PostgreSQL-backed ConfigStore

Date: 2026-09-09

## What changed

Migrate the gateway's static configuration (provider registry, pool
definitions, API keys, provider credentials, OAuth credential rotation) from
env-var / Python-file sources into a PostgreSQL-backed `ConfigStore` so
operators can edit live config through the admin REST API without
redeploying.

**Status: IN PROGRESS — implementation phase.** All wiring is in place
(app, admin API, config_runtime, passthrough CAS, tests, canary infra).
The only remaining piece is the `tusker_gateway/config_store.py` body,
which is still a stub.

## Why

Today every configuration change requires a `kubectl edit` of
`k8s/deployment.yaml` (or `tusker-env-vault`), a `rsync` + `deploy.sh`, and
a `rollout restart`. That is a 5–10 minute round-trip for a single API key
rotation. The new design:

- Live config edits via `POST/PUT/DELETE /admin/*` with `admin:write` scope.
- A `ConfigRuntime` poll loop watches the store's `generation` counter and
  applies changes to pool manager, catalog registry, capabilities registry,
  token rotators, and identity store without restarting the pod.
- OAuth credential refresh persists back to the DB via a CAS callback
  (`persist_credentials(provider, expected, replacement) -> bool`), so
  refreshed tokens survive pod restarts without touching the Hermes
  auth file.

## Files in this directory

- `IMPLEMENTATION_PLAN.md` — step-by-step checklist for finishing the
  `ConfigStore` body and wiring it into the app.
- `SCHEMA.md` — the PostgreSQL schema (tables, columns, encryption).

## Scope boundaries

| In scope | Out of scope |
|---|---|
| Provider registry CRUD | Image/video/TTS provider config (separate subsystems) |
| Pool definitions CRUD | Rate-limit / budget / cache config (those have their own DBs) |
| API key + identity CRUD | Session/cookie auth state (in-memory, process-local) |
| OAuth credential persistence | OAuth token refresh logic (in passthrough.py) |
| Admin REST API write endpoints | The dashboard SPA (separate frontend) |

## Deployment prerequisites (already satisfied)

- Shared PostgreSQL service `tusker-gateway-postgres` is running in the
  `hermes` namespace and is reachable via `TUSKER_STATE_DATABASE_URL`
  (secret `tusker-gateway-postgres-auth`, key `DATABASE_URL`).
- `pgcrypto` extension is available on the cluster PostgreSQL instance
  (verified during canary provisioning).
- `k8s/config-canary.py` can render/provision/seed/smoke/cleanup a canary
  deployment against an isolated `tusker_gateway_config_canary` database.
- `k8s/deployment.yaml` already exposes `TUSKER_STATE_DATABASE_URL`,
  `TUSKER_STATE_DATABASE_POOL_MAX`, and
  `TUSKER_STATE_DATABASE_CONNECT_TIMEOUT` to the gateway pod.

## Rollout strategy

1. Implement `ConfigStore` body (SQLite + PG paths, idempotent schema,
   encryption, snapshot/reload/CRUD).
2. Add `TUSKER_CONFIG_DATABASE_ENABLED=1` to the canary deployment and
   run `python3 k8s/config-canary.py smoke` to validate the pgcrypto
   round-trip and `/health` + `/ready` against the canary route
   (`:8642`).
3. Seed the canary DB from the live pod's resolved env config
   (`k8s/config-canary.py seed --execute`) and verify the admin
   `/admin/config` snapshot matches the env-derived config.
4. Switch the canary to `TUSKER_CONFIG_DATABASE_ENABLED=1` and run the
   full test suite (including the new `tests/test_config_runtime.py`
   and `tests/test_admin_api.py` write-path tests).
5. When the canary is green, flip `TUSKER_CONFIG_DATABASE_ENABLED=1` in
   `k8s/deployment.yaml`, `rsync` source to `visor`, run `./k8s/deploy.sh`.
6. The first rollout after the flip will use the env-var fallback (empty
   tables) until `migrate_config_to_db.py` is run — backwards-compatible
   by design. Run the migration, then `rollout restart`.

## Verification

End-to-end checks to run after the migration lands:

```
# 1. Store body works against a temp SQLite DB
python3 -m pytest tests/test_config_store.py -q

# 2. Admin write endpoints work against the DB-backed store
python3 -m pytest tests/test_admin_api.py -q

# 3. ConfigRuntime poll loop applies generation changes
python3 -m pytest tests/test_config_runtime.py -q

# 4. Full suite (offline, skip live passthrough tests)
python3 -m pytest tests/ -p no:cacheprovider --ignore=tests/test_passthrough_providers.py -q

# 5. Canary smoke test against live cluster
python3 k8s/config-canary.py smoke --pod tusker-gateway-c... --execute

# 6. Live admin write round-trip
curl -s -H "Authorization: Bearer $ADMIN_KEY" https://ai.tusker.net.au/admin/config | jq .
curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"principal":"don.gould","tenant":"engineering","scopes":["admin:read","admin:write"]}' \
  https://ai.tusker.net.au/admin/keys
```