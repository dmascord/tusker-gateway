# PostgreSQL HA for the Tusker gateway

## Current state

The gateway's shared state database is a single PostgreSQL 16 instance
(`tusker-gateway-postgres`) on a 10 GiB Longhorn RWO PVC. The pod runs on the
`wyzard` node; the Longhorn volume is attached to that node exclusively.

This single-instance setup is fragile:

| Failure mode | Impact |
|---|---|
| Longhorn volume detach | Pod loses RWO mount; gateway enters degraded mode for 2–10 min |
| wyzard node loss | Pod reschedules elsewhere; PVC re-attaches; multi-minute outage |
| OOM on postgres | Pod restarts; recovery from WAL replay (clean if graceful) |
| PostgreSQL config error | Pod can fail to start (see postgres.conf FATAL observed 2026-09-13) |

The 16 recovery events in 20 hours (per the 2026-09-13 review) are
mostly volume flaps and clean WAL replays. Each one incurs degraded mode
(state writes lost, in-memory fallback serves traffic).

## HA roadmap

Three phases, each independently shippable.

### Phase 1 — Resilient single instance (shipped)

Hardens the existing single instance so volume flaps no longer cause
unclean-recovery cycles.

* `k8s/postgres-config.yaml` — ConfigMap with `override.conf` mounted at
  `/etc/postgresql/override`. Pins durability-relevant settings
  (`fsync=on`, `synchronous_commit=on`, `full_page_writes=on`,
  `wal_level=replica`, `max_wal_size=1GB`) so unclean shutdowns replay
  cleanly. An initContainer injects `include_if_exists` into the
  auto-managed `postgresql.conf`.
* `k8s/gateway-postgres.yaml` — added a `lifecycle.preStop` hook that
  triggers `pg_ctl … -m fast stop -t 60` before the 90s
  `terminationGracePeriodSeconds` elapses. K8s SIGTERM hits, postgres
  flushes WAL, the volume detaches cleanly.
* `k8s/postgres-snapshot-job.yaml` — Longhorn `RecurringJob` snapshots the
  postgres PVC every 15 minutes (96 retained = 24h of RPO). Supplements
  the cluster-wide daily-snapshot job.

**RPO after Phase 1:** ≤15 minutes (snapshot interval).

### Phase 2 — Hot-standby with streaming replication (not yet shipped)

Adds a second pod that runs `pg_basebackup` once at startup, then enters
standby mode (`primary_slot_name = 'standby_1'`, `hot_standby = on`).
WAL is streamed over a Service that fronts the primary. A K8s Lease
designates primary vs standby; on primary pod loss, the standby runs
`pg_ctl promote` and updates the Lease.

**Operational steps:**
1. Add `Secret` `tusker-gateway-postgres-replication` with a
   replication-role user (e.g. `standby_replicator` with `REPLICATION`
   privilege).
2. Add a `StatefulSet` of replicas 2 with `podAntiAffinity` so the two
   pods cannot land on the same node.
3. Each pod's initContainer waits until it can read the Lease and
   determine its role. Primary runs `postgres`; standby runs
   `pg_basebackup -Fp -Xs -R -D /var/lib/postgresql/data/pgdata
   --checkpoint=fast` then `postgres`.
4. Add a `Service` with `publishNotReadyAddresses: true` and selector
   `role=primary`. The primary pod labels itself; standby does not.
5. Add an exec-based sidecar that updates the Lease every 5s; on lease
   expiry (primary dead > 15s), the standby promotes itself.
6. Document a manual `kubectl -n hermes exec tusker-gateway-postgres-1 --
   pg_ctl promote` procedure as the override for automated failover.

**RPO after Phase 2:** seconds (WAL streaming); **RTO:** ~10s.

**Costs:** +1 pod, +10 GiB PVC (longhorn snapshot), +complexity in the
state-migration scripts, +a custom Service/Endpoint controller. Out of
scope for the current review; document here for the next planning
cycle.

### Phase 3 — External managed Postgres (future)

If a managed Postgres (Cloud SQL, RDS, Crunchy Bridge, etc.) becomes
available on `visor`, move the gateway's state store off Longhorn
entirely. The gateway already accepts a Postgres DSN via
`TUSKER_STATE_DATABASE_URL` and `TUSKER_KEY_ENCRYPTION_KEY`; only the
Secret values change.

**Costs:** monthly DB spend, possible egress if the cluster and managed
DB are in different regions, requires the gateway's network policy to
allow egress to the managed endpoint.

## Recovery procedure (Phase 1)

After a node loss, volume flap, or pod crash:

1. `kubectl -n hermes get pods -l app=tusker-gateway-postgres -o wide`
   — confirm the pod is Running and Ready.
2. `kubectl -n hermes logs deploy/tusker-gateway-postgres --since=10m`
   — confirm WAL recovery completed (`database system is ready to accept
   connections`).
3. If WAL replay fails, restore from the latest snapshot:
   ```bash
   # List snapshots for the postgres volume
   kubectl -n longhorn-system get snapshot -l app=tusker-gateway-postgres
   # Restore (clone the snapshot to a new PVC and patch the deployment)
   # Longhorn snapshot restore: see Longhorn docs.
   ```
4. `kubectl -n hermes exec deploy/tusker-gateway -- curl -sf
   http://127.0.0.1:8642/health | jq .state_store` should report
   `degraded=false`, `recovery_count` incrementing by 1.

## Why not Patroni / Zalando / Crunchy operator?

The cluster does not run any Postgres operator today. Adding one means
CRDs, RBAC, and a namespace-scoped install per cluster — substantial
up-front cost. Phase 1 closes the immediate reliability gap
(RPO ≤15 min); Phase 2 is a self-contained addition without an operator.

## Why not Longhorn RWX + concurrent postgres?

Longhorn's RWX export is backed by an NFS server. PostgreSQL on NFS is
discouraged for write-heavy workloads (write-fsync semantics depend on
local fsync, NFS can lose acknowledged writes on a server crash). The
existing RWO + preStop pattern is safer for now.
