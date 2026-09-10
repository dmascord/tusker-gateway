# Project TODOs

Active work items that span multiple sessions / PRs. Items live here
until they ship and then move to a release-notes doc.

## DB-backed config store (in progress)

Migrate gateway static config (provider registry, pool definitions, API
keys, provider credentials, OAuth token rotation) from env-var / Python
sources into a PostgreSQL-backed `ConfigStore`.

**Status:** wiring complete (app, admin REST endpoints, runtime reload,
OAuth CAS, canary infra, tests). Only the `tusker_gateway/config_store.py`
body remains stubbed.

**Full plan:** `docs/migrations/2026-09-09-config-db/IMPLEMENTATION_PLAN.md`
**Schema spec:** `docs/migrations/2026-09-09-config-db/SCHEMA.md`
**Why:** `docs/migrations/2026-09-09-config-db/README.md`

### Open items (in dependency order)

- [ ] Implement `ConfigStore` body (SQLite + PG paths, idempotent schema,
      encryption, snapshot/reload/CRUD).  See `IMPLEMENTATION_PLAN.md` §1.
- [ ] Fix `ConfigUnavailableError` to accept `code=` kwarg
      (currently raises `TypeError` at module import in the stub).
- [ ] Implement `persist_credentials` as a CAS callback
      `(provider, expected, replacement) -> bool` (currently returns
      `False`, which would silently break OAuth token refresh).
- [ ] Add `tusker_gateway/tools/migrate_config_to_db.py` — idempotent
      env-to-DB migration. See `IMPLEMENTATION_PLAN.md` §2.
- [ ] Drop the fake-module injection from `tests/test_config_runtime.py`
      once the real store exists. See `IMPLEMENTATION_PLAN.md` §3.
- [ ] Canary smoke-test against `tusker-gateway-c` pod via
      `k8s/config-canary.py smoke --execute`. See `IMPLEMENTATION_PLAN.md`
      §4.
- [ ] Flip `TUSKER_CONFIG_DATABASE_ENABLED=1` in `k8s/deployment.yaml`,
      run migration, `rsync`, `./k8s/deploy.sh`.
- [ ] Live admin write round-trip: `POST /admin/keys` for Don Gould,
      verify request through `https://ai.tusker.net.au/v1/chat/completions`.
- [ ] Update `docs/postgresql-state.md` to list OAuth credentials and
      provider secrets as state-DB managed.
- [ ] Update `AGENTS.md` doc index.

### Test commands

```
# Single-file (currently passes):
python3 -m pytest tests/test_admin_api.py -q                  # 15 passed
python3 -m pytest tests/test_config_runtime.py -q             # 13 passed (in isolation)

# Full suite (5 fail in test_config_runtime.py under ordering pollution):
python3 -m pytest tests/ -p no:cacheprovider --ignore=tests/test_passthrough_providers.py -q
# → "5 failed, 887 passed, 3 skipped, 1868 warnings in 22.91s"
```

### Test-isolation failure (currently reproduced)

```
FAIL tests/test_config_runtime.py::test_runtime_config_returns_store_snapshot_and_updates_generation
FAIL tests/test_config_runtime.py::test_runtime_config_unavailable_retains_last_good
FAIL tests/test_config_runtime.py::test_identity_config_same_pattern
FAIL tests/test_config_runtime.py::test_reload_now_delegates_to_store
FAIL tests/test_config_runtime.py::test_poll_loop_applies_only_on_generation_change
```

**Root cause (confirmed via test isolation):** `tests/test_config_runtime.py`
replaces `sys.modules["tusker_gateway.config_store"]` at module load,
but `config_runtime.py` line 22 caches the `ConfigStore` class when
imported — so a different test file importing the real store BEFORE
this test pollutes the binding. Five `_store()` calls fail
`isinstance(store, ConfigStore)` and silently return `None`, which
breaks `runtime_config()`, `identity_config()`, `reload_now()`, and
the poll loop.

**Fix:** implement the real `ConfigStore` (item 1 above), then rewrite
the test to use a real SQLite-tempfile instance instead of the fake.
The fake-injection hack should be deleted.

### Destructive actions (require confirmation)

- `kubectl apply -f k8s/config.yaml` (secrets)
- `kubectl rollout restart deployment/tusker-gateway` (DB schema migration
  requires restart to pick up new tables)
- `kubectl delete deployment tusker-gateway-config-canary` after smoke
- `rm data/config.db` (local dev only — never run on a prod pod)
- Any operation that writes to `pgcrypto`-encrypted columns under a
  different encryption key than the existing rows were encrypted with —
  this permanently bricks the row.

