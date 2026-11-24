# Database-backed audit chain

The gateway's append-only, integrity-chained audit events are written to the
shared state database (PostgreSQL in production) instead of a single JSONL file
on the `tusker-home-rwx` Longhorn/NFS volume. The change was driven by the
2026-11-24 incident: a stale NFS `flock` returned `EIO`, and because audit is
fail-closed, healthy requests were aborted and mislabelled as upstream errors.

## Design

### Tables

- `audit_chain_state` — singleton row (`id = 1`) holding `head_hash`,
  `head_sequence`, and the retention anchor `tail_hash` / `tail_sequence`.
- `audit_events` — one row per event with `sequence`, `event_id`,
  `event_type`, `request_id`, `timestamp`, `chain_version`, `chain_mode`,
  `previous_hash`, `hash`, JSON `payload`, and `created_at`.

### Append

`AuditLogger.write()` / `write_sync()` are unchanged; `_append()` dispatches to
the database backend. An append:

1. Opens a short transaction and `SELECT ... FOR UPDATE` the singleton chain
   row. That serializes concurrent writers across processes and pods; SQLite
   achieves the same with a `BEGIN IMMEDIATE`-style transaction.
2. Reads `head_hash`, computes the next sequence, and builds the same bounded
   payload the file backend used (timestamp, policy version, chain mode).
3. Computes `hash = digest(previous_hash, canonical_event)` — SHA-256 or HMAC
   over the canonical JSON, identical to the file format.
4. Inserts the event row and advances `head_hash` / `head_sequence` in the
   same transaction.

The hash-chain guarantees are preserved: `previous_hash` links each row to its
predecessor, and `verify_db()` walks the whole chain from the retention anchor.

### Retention

`TUSKER_AUDIT_RETENTION_DAYS` (default 90) bounds growth without breaking
verification. `prune()` deletes rows older than the window and updates
`tail_hash` / `tail_sequence` to the last pruned row, so verification resumes
at the first retained row using its true predecessor hash. Pruned history is
no longer verifiable — that is the explicit retention trade-off.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `TUSKER_AUDIT_BACKEND` | `database` | `database` or legacy `file` |
| `TUSKER_AUDIT_DATABASE_PATH` | `~/.hermes/audit.db` | Logical target; resolves to the shared DSN in postgres mode |
| `TUSKER_AUDIT_RETENTION_DAYS` | `90` | Row retention window |
| `TUSKER_AUDIT_FAIL_CLOSED` | `false` | Unchanged semantics: raise `AuditWriteError` when persistence fails |
| `TUSKER_AUDIT_HMAC_KEY` | — | Unchanged: switches chain to HMAC-SHA256 |
| `TUSKER_AUDIT_LOG_PATH` | — | File backend path; kept as the **migration source** only |

`storage.py` resolves the logical target to `TUSKER_STATE_DATABASE_URL` in
production; local dev and tests use SQLite. When `fail_closed` is on, the store
uses the `critical` fallback policy (an outage raises); when off, `advisory`
(an outage degrades to "no audit event" without failing the request).

## Migration

Historical JSONL records are imported with their original sequence numbers and
hashes, so the verified chain is continuous:

```bash
# From visor, with the usual state-DB env vars set:
python -m tusker_gateway.tools.migrate_audit_to_db \
    --file /home/tusker/.hermes/audit.jsonl --dry-run
python -m tusker_gateway.tools.migrate_audit_to_db \
    --file /home/tusker/.hermes/audit.jsonl
```

The tool verifies the source chain first and refuses to import into a
non-empty audit table (imported rows must be first to keep sequence order), so
it must be run **before** the first database-backed event is written. It
inserts in bounded batches.

For the 2026-11-24 rollout the historical file was left in place as an
independently verifiable record (`verify_audit <path>`) and the database chain
starts fresh from the deploy. That keeps the two chains simple to reason about
instead of risking a partial import while live traffic was still appending to
the file. Other environments that need one continuous chain can run the
migration from a maintenance window before enabling `database` backend.

## Verification and operations

```bash
python -m tusker_gateway.tools.verify_audit --database --json
```

`/health` reports `enterprise_controls.audit_backend` alongside the existing
`audit_integrity` and `audit_fail_closed` fields.

A database outage with `fail_closed=true` still fails requests closed — the
control's intent is unchanged — but the failure mode is now a transactional DB
error, not an NFS lock error, and the misleading "Upstream provider error"
wording was removed in the same release (see the 2026-11-24 incident doc).

## Rationale and trade-offs

- **Removes the shared-file lock:** no `flock` over NFS, so stale share-manager
  lock state can no longer take down live traffic.
- **Cross-process correctness:** the database row lock is stronger than the
  advisory file lock, and works if the gateway is ever scaled past one replica.
- **Retention is explicit:** bounded rows with a verification anchor instead of
  an unbounded 740 MB file on a 5 GiB RWX volume.
- **Dependency shift:** audit now shares the state database's availability
  envelope. Operators must treat `tusker-gateway-postgres` as part of the
  audit control; the old "audit volume" operational surface disappears.
