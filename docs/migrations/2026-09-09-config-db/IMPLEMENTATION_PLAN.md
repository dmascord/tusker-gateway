# Config DB implementation plan

This plan finishes the `tusker_gateway/config_store.py` body. All
adjacent work (app wiring, admin REST endpoints, runtime reload, OAuth
persistence CAS, canary infrastructure, tests) is already in place — only
the store body is stubbed.

## Test-driven contract

`tests/test_config_runtime.py` and `tests/test_admin_api.py` already
encode the public contract. They import `tusker_gateway.config_store`
`ConfigStore` and `ConfigUnavailableError`. They are currently failing in
two ways:

1. **`config_store.py` is a stub.** Every mutating method raises
   `ConfigUnavailableError("config store unavailable", code="...")`. The
   class declaration `class ConfigUnavailableError(Exception)` is a plain
   `Exception`, so the `code=` kwarg actually raises `TypeError` instead
   of the intended `ConfigUnavailableError`. `admin.py` correctly raises
   WITHOUT the kwarg; the stub itself does not.

2. **Fake-module injection is fragile.** `tests/test_config_runtime.py`
   replaces `sys.modules["tusker_gateway.config_store"]` with a scriptable
   fake at module load time, but `config_runtime.py` line 22 does
   `from tusker_gateway.config_store import ConfigStore, ...` at its OWN
   import time. If any other test imports the real module first, the
   real classes get bound inside `config_runtime`, and the fake stores
   fail `isinstance` checks downstream. The 5 failing tests
   (`test_runtime_config_returns_store_snapshot_and_updates_generation`,
   `test_runtime_config_unavailable_retains_last_good`,
   `test_identity_config_same_pattern`,
   `test_reload_now_delegates_to_store`,
   `test_poll_loop_applies_only_on_generation_change`) all reproduce the
   same root cause.

The fix for both is implementing the real store; then the test file can
be simplified to use the real store against a temp SQLite DB and the
fake-injection hack can be deleted.

## Phase 1: ConfigStore body (SQLite + PG paths)

Replace `tusker_gateway/config_store.py`. Target length: ~700 lines.

### 1.1 Imports and exception class

- `from __future__ import annotations`
- Imports: `asyncio`, `base64`, `hashlib`, `json`, `logging`, `os`,
  `secrets`, `threading`, `time`; from project: `storage.shared_database`,
  `identity.CallerIdentity`, `identity.IdentityConfig`,
  `identity.fingerprint_api_key`.
- `class ConfigUnavailableError(Exception)` — add an explicit
  `__init__(self, message: str = "", *, code: str | None = None)` so it
  accepts `code=` without crashing. Keep `.message` and `.code` as
  attributes; do NOT inherit from a custom base that breaks isinstance
  matching against the stub's bare-Exception declaration (which the tests
  currently redefine anyway).

### 1.2 Connection management

- `__init__` accepts `database`, `fallback_config`, `fallback_identity_config`,
  `_env` (default `os.environ`). When `TUSKER_CONFIG_DATABASE_ENABLED` is
  falsy and `database` is None, store remains inert (legacy mode).
- Lazy DB accessor: call `shared_database()` from `tusker_gateway.storage`
  on first use; cache the resulting `DatabaseConnection`. Detect PG vs.
  SQLite by inspecting `self._database.is_postgres`.
- On SQLite, refuse encrypt ops and either:
  (a) write plaintext to the `*_encrypted` columns (dev-only, log a
  WARNING), or
  (b) raise if `TUSKER_CONFIG_DATABASE_ENABLED=1` AND
  `_require_managed=True` (default in prod paths).
  Decision: option (a) for `tests/`, option (b) for prod (gated by
  `TUSKER_ENV=production`, but never set this env var — instead
  introduce `_managed: bool` arg that admin wires as `True`).

### 1.3 Schema bootstrap

`_bootstrap_schema(self) -> None`:
- If PG: `CREATE EXTENSION IF NOT EXISTS pgcrypto;` first.
- `CREATE TABLE IF NOT EXISTS` for the seven tables (`tusker_config_meta`,
  `_providers`, `_provider_settings`, `_provider_api_keys`, `_pools`,
  `_client_keys`, `_oauth_credentials`).
- For SQLite: inline the encryption key column as plain TEXT and skip
  `pgcrypto`.
- INSERT the `meta` row if missing (`INSERT … ON CONFLICT DO NOTHING` on
  PG; `INSERT OR IGNORE` on SQLite).

### 1.4 Encryption helpers

- `_encrypt(self, plaintext: str) -> str` — `pgp_sym_encrypt` on PG;
  passthrough on SQLite.
- `_decrypt(self, ciphertext: str) -> str` — `pgp_sym_decrypt` on PG;
  passthrough on SQLite.
- `_key(self) -> str` — read `TUSKER_KEY_ENCRYPTION_KEY` from
  `self._env` (canary secret) or fallback to a fixed env var name. Raise
  `ConfigUnavailableError("encryption key missing", code="encryption_key_missing")`
  if PG mode and the key is empty.

### 1.5 In-memory snapshot cache

- `self.generation: int` — bump on every successful write.
- `self._runtime_cfg: dict | None` and `self._runtime_identity_cfg: IdentityConfig | None`
  — last snapshot used by `runtime_config()` / `identity_config()`.
- The `_apply(generation)` reload helper reads all tables, builds the
  snapshot, swaps the dicts, then sets `generation`.

### 1.6 Public methods (target signatures)

Match what `admin.py`, `auth.py`, and `config_runtime.py` already call.
Skeletons below — fill with real implementations.

```
def runtime_config(self, fallback: dict) -> dict
def identity_config(self, fallback: IdentityConfig) -> IdentityConfig
def snapshot(self) -> dict                 # admin /admin/config GET
def reload_now(self) -> bool              # sync one generation; returns True if changed
def purge_expired(self) -> int            # maintenance; oauth token TTL
def hydrate(self, tracker) -> int         # one-shot loader for daemon stats; no-op stub
def hydrate_providers(self, tracker) -> int
def upsert_provider(self, body: dict) -> dict
def delete_provider(self, provider: str) -> None
def upsert_provider_settings(self, provider: str, body: dict) -> dict
def upsert_provider_credentials(self, provider: str, body: dict) -> dict
def upsert_pool(self, body: dict) -> dict
def delete_pool(self, pool: str) -> None
def upsert_client_key(self, body: dict) -> dict      # also supports "rotate" via body flag
def revoke_client_key(self, fingerprint: str) -> None
def rotate_client_key(self, fingerprint: str) -> dict      # issues new key, returns body
def resolve(self, api_key: str) -> CallerIdentity | None     # fingerprint lookup
@property persist_credentials(self) -> Callable | bool      # CAS callback for OAuth refresh
```

`persist_credentials` returns a callable
`(provider, expected, replacement) -> bool`, not a bool. The current
stub returns `False` which is incorrect; `passthrough.py` line 854
expects a callable.

### 1.7 Error envelope

- `snapshot()` should NEVER raise; on DB error it returns
  `{"_error": "config_store_unavailable", "providers": {},
   "provider_settings": {}, "provider_api_keys": {},
   "pools": {}, "client_keys": {}, "oauth_credentials": {}}`.
- Mutating methods raise `ConfigUnavailableError(message, code="...")`.
  Callers (`admin.py`) already catch this and surface a `503`.

## Phase 2: Migration script

Create `tusker_gateway/tools/migrate_config_to_db.py`:

- Reads `APP["config"]` (the env-derived dict the app builds at startup).
- For each section (providers, pools, api_keys, identity profiles),
  idempotently INSERTs missing rows into the corresponding table.
- Prints a CSV-like summary of (`section`, `inserted`, `skipped`,
  `errors`).
- Idempotent: re-run is a no-op (all rows already exist).
- `--dry-run` flag for staging validation.

## Phase 3: Test cleanup

Once the real store is in place:

1. Edit `tests/test_config_runtime.py` to drop the fake-module injection
   block (lines 26–61). Replace the fake `ConfigStore` class with the
   real `ConfigStore(fallback_config={}, fallback_identity_config=IdentityConfig())`
   configured against a `tempfile.NamedTemporaryFile(suffix=".db")`.
2. Verify the 5 currently-failing tests now pass under both:
   - `pytest tests/test_config_runtime.py -q` (single file)
   - `pytest tests/ -p no:cacheprovider --ignore=tests/test_passthrough_providers.py -q`
     (full suite — the ordering issue disappears).

## Phase 4: Deploy & verify

1. Run `python3 k8s/config-canary.py render --image-digest <digest>` to
   produce `k8s/config-canary.yaml` with `TUSKER_CONFIG_DATABASE_ENABLED=1`.
2. Apply canary, then `provision` + `seed` against `tusker-gateway-c`.
3. `smoke` the canary: pgcrypto round-trip + HTTP `/health` + admin
   `/admin/config` GET.
4. After canary passes, flip `TUSKER_CONFIG_DATABASE_ENABLED=1` in
   `k8s/deployment.yaml`, run `migrate_config_to_db.py` against
   prod's DB, then `./k8s/deploy.sh`.
5. Verify backwards compat: stop the store on startup → app keeps running
   on env fallback. Doc in `docs/deployment-k8s.md`.

## Phase 5: Cleanup

- Delete the fake-module injection from `tests/test_config_runtime.py`
  (final cleanup, separate from Phase 3 — keep the file runnable
  during the migration).
- Remove the `config_latch`/`config_db_keys_authoritative` plumbing in
  `auth.py` once the store proves reliable (open follow-up; not in this
  PR).
- Update `docs/postgresql-state.md` to add OAuth credentials and provider
  secrets to the state-DB list.
- Update `AGENTS.md` doc index to point at
  `docs/migrations/2026-09-09-config-db/` and the new `TODO.md`.

## Out of scope (clearly tracked)

- Migration of `data/provider_usage.db` into PG (already done; not part
  of config store).
- Image/video/TTS provider config stores (separate subsystems).
- Admin SPA write forms (separate frontend PR).
- Refactor of `auth.py` `config_latch` — wait until store has been in
  prod for ≥1 week.

