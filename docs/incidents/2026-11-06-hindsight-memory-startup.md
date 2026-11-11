# Hindsight memory startup failure (2026-11-06)

## Symptom

The `hindsight` Deployment in namespace `hindsight` entered `CrashLoopBackOff`, so the gateway's cluster-internal memory backend was unavailable. The Hindsight pod was running image `ghcr.io/vectorize-io/hindsight@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06` (v1.1.0).

## Cause

A live Deployment patch added:

```text
HINDSIGHT_API_DATABASE_URL=postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight
```

Hindsight was configured to use embedded `pg0`, whose port is auto-assigned and whose server is started during `MemoryEngine.initialize()`. The explicit URL therefore caused migrations to connect to fixed port 5432 before/while pg0 was available. The failing pod logged:

```text
psycopg2.OperationalError: connection to server at "127.0.0.1", port 5432 failed: Connection refused
ValueError: Database URL is required for migrations
```

The second error appeared after the failed external-URL override was removed from the configuration path while the engine still used the embedded database lifecycle.

## Resolution

The invalid `HINDSIGHT_API_DATABASE_URL=postgresql://...@127.0.0.1:5432/...`
override was removed first, restoring Hindsight to its documented embedded
`pg0` default. That recovered the API without deleting or changing the
`hindsight-data` PVC.

The later migration introduced a real external PostgreSQL Service and
Secret-backed URL; the current Deployment intentionally uses that URL. The
original `127.0.0.1:5432` override remains invalid for embedded pg0 and MUST
NOT be reintroduced.

## Verification

- Initial recovery: `kubectl -n hindsight rollout status deployment/hindsight --timeout=30s` succeeded; recovery logs showed `Database: pg0` and `PostgreSQL started`.
- Post-migration verification is recorded below; the live Hindsight Deployment now uses the dedicated PostgreSQL Service.

## Follow-up: embedded PostgreSQL migration

The Hindsight API was migrated to a dedicated `pgvector/pgvector:pg18`
StatefulSet at `hindsight-postgres:5432` using PVC `hindsight-postgres-data`
and Secret `hindsight-postgres-auth`. The embedded `hindsight-data` PVC remains
Bound and was retained as rollback storage.

Migration evidence:

- Source custom-format dump: `/Volumes/dev/hindsight-pg0-20261106.dump`, 866 MiB.
- Restored target counts: `banks=4`, `memory_units=150313`, `chunks=23300`,
  `documents=901`.
- Target extension: `vector 0.8.7`.
- Hindsight startup logged the Service-backed URL (credentials masked),
  completed migrations, rebuilt missing text indexes, initialized the
  PostgreSQL pool, and reached Ready.
- Hindsight `/health` from the gateway pod returned HTTP 200 with
  `database=connected`.
- Restarting `hindsight-postgres` preserved `memory_units=150313` and
  `banks=4`; the Hindsight health probe remained successful afterward.

The external Hindsight Deployment was patched live because its authoritative
manifest is outside this repository. Keep the database URL Secret-backed and
do not reintroduce an embedded `pg0` URL override or `127.0.0.1:5432`.

The Hindsight Deployment is managed outside this gateway repository. The live correction is therefore recorded here as an operational incident; if that external manifest is reapplied, it must continue to omit the invalid database URL.
