# Config DB schema

PostgreSQL tables live in the `public` schema of the
`tusker_gateway` database (or `tusker_gateway_config_canary` for the
canary). All tables share the `tusker_config_` prefix and are created
via `CREATE TABLE IF NOT EXISTS`. Local dev and CI use a SQLite file
(`data/config.db`) with the same logical schema (encryption skipped,
plaintext columns).

## Encryption

Provider API keys, OAuth refresh tokens, and admin-managed client keys
are encrypted at rest on PostgreSQL via `pgcrypto`'s
`pgp_sym_encrypt(plain, TUSKER_KEY_ENCRYPTION_KEY)` / `pgp_sym_decrypt(cipher, ...)`.
On SQLite (dev/test) the columns hold plaintext and the encryption key is
NOT consulted; this is documented as dev-only and the prod path refuses
SQLite backends when `TUSKER_CONFIG_DATABASE_ENABLED=1`.

Encryption key source: `TUSKER_KEY_ENCRYPTION_KEY` env var
(or `tusker-gateway-config-canary-encryption` secret in canary mode).
A missing key in prod is fatal (raises on first encrypt call).

## Tables

### `tusker_config_meta`

Singleton row holding the current generation counter. The poll loop
compares the live row value with the in-process value and only applies
when the counter changes.

```sql
CREATE TABLE IF NOT EXISTS tusker_config_meta (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    generation      BIGINT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `tusker_config_providers`

One row per provider (mirrors a `ProviderEndpoint` from
`config.DEFAULT_PROVIDER_REGISTRY`).

```sql
CREATE TABLE IF NOT EXISTS tusker_config_providers (
    name                TEXT PRIMARY KEY,
    base_url            TEXT NOT NULL,
    chat_path           TEXT NOT NULL DEFAULT '/v1/chat/completions',
    auth_env            TEXT,
    pool_env            TEXT,
    model_header        TEXT,
    models_path         TEXT,
    rerank_path         TEXT,
    model_aliases       TEXT,   -- JSON object
    zdr_ok              INTEGER NOT NULL DEFAULT 0,
    heavyweight         INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `tusker_config_provider_settings`

Runtime toggles per provider.

```sql
CREATE TABLE IF NOT EXISTS tusker_config_provider_settings (
    provider            TEXT PRIMARY KEY REFERENCES tusker_config_providers(name) ON DELETE CASCADE,
    enabled             INTEGER NOT NULL DEFAULT 1,
    disabled_cause      TEXT,
    passthrough_disabled INTEGER NOT NULL DEFAULT 0,
    disabled_provider   INTEGER NOT NULL DEFAULT 0,
    heavyweight_only    INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `tusker_config_provider_api_keys`

Provider API keys. Encrypted at rest on PG.

```sql
CREATE TABLE IF NOT EXISTS tusker_config_provider_api_keys (
    provider            TEXT PRIMARY KEY REFERENCES tusker_config_providers(name) ON DELETE CASCADE,
    key_encrypted       TEXT NOT NULL,                 -- pgp_sym_encrypt(plaintext, key) or plaintext on SQLite
    fingerprint         TEXT NOT NULL,                  -- sha256 of plaintext, hex
    last4               TEXT NOT NULL,                   -- last 4 chars of plaintext
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `tusker_config_pools`

Pool definitions (mirrors a `PoolConfig`).

```sql
CREATE TABLE IF NOT EXISTS tusker_config_pools (
    name                TEXT PRIMARY KEY,
    models              TEXT NOT NULL,                  -- JSON array of model spec dicts
    context_window      INTEGER NOT NULL DEFAULT 128000,
    zdr                 INTEGER NOT NULL DEFAULT 0,
    provider_warmup_secs INTEGER NOT NULL DEFAULT 300,
    auto_free           INTEGER NOT NULL DEFAULT 0,
    heavyweight_only    INTEGER NOT NULL DEFAULT 0,
    auto_catalog_providers TEXT,                         -- JSON array
    fallback_pools      TEXT,                            -- JSON array
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `tusker_config_client_keys`

Admin-managed caller API keys + identity profiles.

```sql
CREATE TABLE IF NOT EXISTS tusker_config_client_keys (
    fingerprint         TEXT PRIMARY KEY,                -- sha256 of the raw key
    principal           TEXT NOT NULL,
    tenant              TEXT NOT NULL,
    scopes              TEXT NOT NULL DEFAULT '[]',      -- JSON array
    allowed_pools       TEXT NOT NULL DEFAULT '["*"]',   -- JSON array
    allowed_models      TEXT NOT NULL DEFAULT '["*"]',   -- JSON array
    allowed_providers   TEXT NOT NULL DEFAULT '["*"]',   -- JSON array
    revoked             INTEGER NOT NULL DEFAULT 0,
    api_key_encrypted   TEXT NOT NULL,                    -- raw API key, encrypted
    api_key_last4       TEXT NOT NULL,                    -- last 4 chars
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `tusker_config_oauth_credentials`

OAuth credential pool entries (Codex, Copilot). Used by the
`persist_credentials` CAS callback when `auto_free` is on or when admins
add/replace a credential through `/admin/providers/{provider}/credentials`.

```sql
CREATE TABLE IF NOT EXISTS tusker_config_oauth_credentials (
    provider            TEXT PRIMARY KEY,
    credentials         TEXT NOT NULL,                   -- JSON array of credential dicts, pgp_sym_encrypt on PG
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

## Generation semantics

`generation` in `tusker_config_meta` increments on every write through
the `ConfigStore`. The poll loop in `ConfigRuntime._poll`:

1. Loads `generation` from the DB.
2. Compares with `ConfigRuntime._generation`.
3. If different, fetches the new snapshot and applies it to pool manager,
   catalog, capabilities, rotators, identity store.
4. Stores the new generation.

Reads (`runtime_config`, `identity_config`) use an in-process cache keyed
by generation; if the generation is unchanged the cache returns the
previous snapshot without a DB round-trip. The cache is invalidated on
each reload.

## Failure modes

- DB unreachable: `runtime_config(fallback)` returns the in-process
  last-good snapshot; on first failure returns the env-var fallback.
  `ConfigRuntime._error` is set to the redacted exception class name
  (raw messages are NOT propagated to the operator surface — secrets
  may appear in the message).
- Encryption key missing (PG): the first `upsert_*` call that needs to
  encrypt a secret raises `ConfigUnavailableError("encryption key missing")`.
  Reads from a previously-encrypted row will fail to decrypt with the
  same error.
- Schema drift: table creation is idempotent. New columns require a
  migration script (`tools/migrate_config_schema.py`).

