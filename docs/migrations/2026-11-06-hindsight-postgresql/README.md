# Hindsight PostgreSQL migration

## Scope

Move Hindsight from embedded `pg0` inside the API pod to the dedicated
`hindsight-postgres` StatefulSet in `k8s/hindsight-postgres.yaml`.

The current embedded data is on PVC `hindsight-data` in namespace `hindsight`.
That PVC is the rollback source and MUST NOT be deleted until the migration has
been validated and the retention window has expired.

## Prerequisites

- The Hindsight API is healthy and the current pod is `1/1 Ready`.
- The `hindsight-postgres-auth` Secret exists in namespace `hindsight` with a
  `POSTGRES_PASSWORD` key. Create it out of band:

```bash
PASSWORD="$(openssl rand -hex 32)"
 kubectl -n hindsight create secret generic hindsight-postgres-auth \
  --from-literal=POSTGRES_PASSWORD="$PASSWORD" \
  --from-literal=DATABASE_URL="postgresql://hindsight:${PASSWORD}@hindsight-postgres:5432/hindsight" \
  --dry-run=client -o yaml | kubectl apply -f -
```

- `pgvector/pgvector:pg18` is pullable by the cluster.
- The Hindsight image includes `hindsight-admin` and has network access to the
  PostgreSQL Service during the cutover.

## Migration procedure

### 1. Validate and provision the target

```bash
kubectl apply --dry-run=server -f k8s/hindsight-postgres.yaml
kubectl apply -f k8s/hindsight-postgres.yaml
kubectl -n hindsight rollout status statefulset/hindsight-postgres --timeout=10m
kubectl -n hindsight get pod hindsight-postgres-0 -o wide
```

The target is intentionally a separate database and PVC. Do not reuse the
`hermes/tusker-gateway-postgres` database: Hindsight's vector and maintenance
workload should have an independent failure and resource boundary.

### 2. Stop Hindsight writers and create a logical backup

The embedded database is a PostgreSQL server managed by Hindsight's `pg0`.
Use PostgreSQL's custom-format `pg_dump` while that server is running; do not
copy the live PostgreSQL data directory. Keep the embedded PVC and the dump
until the target has passed the persistence checks.

Capture the source pod and create a dump inside the pod:

```bash
SOURCE_POD="$(kubectl -n hindsight get pods -l app=hindsight \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl -n hindsight exec "$SOURCE_POD" -- sh -c \
  'PGPASSWORD=hindsight /home/hindsight/.pg0/installation/18.1.0/bin/pg_dump \
   -h 127.0.0.1 -p 5432 -U hindsight -d hindsight -Fc --no-owner --no-acl \
   -f /tmp/hindsight.dump'
kubectl -n hindsight cp "$SOURCE_POD:/tmp/hindsight.dump" \
  ./hindsight-pg0-backup.dump
```

Do not scale the API down before this command: the API pod owns the embedded
server. The API is quiesced during the actual cutover, after the target has
been restored. If a consistent point-in-time backup is required, stop writers
at the application layer first and then run `pg_dump`; do not stop pg0 before
the dump.

Record the dump checksum and size before proceeding:

```bash
shasum -a 256 hindsight-pg0-backup.dump
ls -lh hindsight-pg0-backup.dump
```

The dump used for this migration was `/Volumes/dev/hindsight-pg0-20261106.dump`.
It was 866 MiB and contained the source counts `banks=4`,
`memory_units=150313`, `chunks=23300`, and `documents=901`.

### 3. Initialize and restore the target

The target database is initialized with the `vector` extension, then restored
from the custom-format dump. A fresh target database is required because
`pg_restore` creates the schema and data; `--exit-on-error` makes failures
fatal.

```bash
PASSWORD="$(kubectl -n hindsight get secret hindsight-postgres-auth \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)"
kubectl -n hindsight exec hindsight-postgres-0 -- env \
  PGPASSWORD="$PASSWORD" psql -U hindsight -d postgres \
  -c 'DROP DATABASE hindsight;' -c 'CREATE DATABASE hindsight OWNER hindsight;'
kubectl -n hindsight exec hindsight-postgres-0 -- env \
  PGPASSWORD="$PASSWORD" psql -U hindsight -d hindsight \
  -c 'CREATE EXTENSION IF NOT EXISTS vector;'
cat ./hindsight-pg0-backup.dump | kubectl -n hindsight exec -i \
  hindsight-postgres-0 -- env PGPASSWORD="$PASSWORD" pg_restore \
  -U hindsight -d hindsight --no-owner --no-acl --exit-on-error
```

Run Hindsight migrations on startup after restore. The migration creates any
new indexes and confirms the target has pgvector. Verify the restored counts
before cutover. `restore` from the Hindsight admin CLI is destructive and is
not used for this PostgreSQL custom-format migration.

`DROP DATABASE`/`pg_restore` is safe only for a newly provisioned target. Never
run it against the embedded source or a target containing unverified data.


### 4. Cut over the API

Patch the external Hindsight Deployment with the Secret-backed URL. The URL
MUST reference the Service, never `127.0.0.1`:

```yaml
- name: HINDSIGHT_API_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: hindsight-postgres-auth
      key: DATABASE_URL
- name: HINDSIGHT_API_MIGRATION_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: hindsight-postgres-auth
      key: DATABASE_URL
```

Remove the `/home/hindsight/.pg0` volume mount from the API pod only after the
external database is validated. Keeping the old mount for the first cutover is
acceptable, but it MUST NOT be used as an implicit fallback: the explicit
external URL is authoritative.

Wait for readiness and inspect startup logs for the external database URL
(masked), successful migrations, and no pg0 startup:

```bash
kubectl -n hindsight rollout status deployment/hindsight --timeout=15m
kubectl -n hindsight logs deploy/hindsight --since=15m | grep -E \
  'Database:|Migration|PostgreSQL started|Application startup|ERROR'
```

## Acceptance checks

Exercise the live service through both the gateway and the Hindsight Service:

```bash
kubectl -n hermes exec deploy/tusker-gateway -- \
  curl -fsS http://hindsight.hindsight.svc.cluster.local:8888/health
kubectl -n hindsight run curl --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 -- \
  curl -fsS http://hindsight:8888/health/ready
```

Using an authenticated gateway key, verify `/v1/memory/health`, list the
known banks, recall a known fact, retain a unique migration sentinel, recall
that sentinel, and delete the sentinel if the caller's policy permits it.
Also verify row counts/bank IDs in the target PostgreSQL database and run
`hindsight-admin repair-bank --all` after restore.

Restart the Hindsight API once. Confirm it becomes Ready without starting pg0,
then repeat the health and recall checks. Only after this persistence check may
the embedded PVC be considered for archival.

## Rollback

If the target migration or API cutover fails:

1. Scale the external Hindsight API down.
2. Remove the external database env entries and restore the previous embedded
   configuration (no `HINDSIGHT_API_DATABASE_URL`).
3. Restore the `/home/hindsight/.pg0` mount to `hindsight-data` if it was
   removed.
4. Scale the API back to one and verify `/health`, `/health/ready`, and memory
   recall.
5. Preserve the target PostgreSQL PVC and migration logs for diagnosis.

Do not delete either PVC during rollback. The embedded source remains the
recovery authority until the target has passed the complete acceptance suite.

## Post-migration operations

The target has the same durability baseline as the gateway PostgreSQL pattern:
RWO storage, clean `preStop`, `fsync`, `synchronous_commit`, `full_page_writes`,
and Longhorn snapshots. Add a dedicated recurring snapshot job before
retiring the embedded PVC. This is still a single PostgreSQL instance; node or
volume loss remains an outage. Streaming replication or an operator-managed
PostgreSQL cluster is a separate availability project.
