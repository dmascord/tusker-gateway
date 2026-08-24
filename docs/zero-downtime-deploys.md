# Zero-downtime deploys

Status: **designed but not yet implemented**. See "Why this doc" below.

## The problem

The gateway deployment uses `strategy.type: Recreate`, which means
every rollout **kills the current pod before the new one starts**.
For chat workloads this drops every in-flight `/v1/chat/completions`
SSE stream mid-flight — clients see "stream closed before a
finish_reason was received".

The reason `Recreate` was chosen: the gateway's only volume
(`tusker-home`, mounted from a `ReadWriteOnce` PVC) prevents two
pods from coexisting on the same data during a rolling update, so
`RollingUpdate` + `maxSurge: 1` would stall forever waiting for
the second mount.

This was fixed once (commit `c2fbf9b`) and then reverted (commit
`369e7f0`) when the PVC constraint was noticed.

## The fix (designed, not landed)

Convert `tusker-home` to `ReadWriteMany` via Longhorn so two pods
can mount the same volume simultaneously. With that, a
`maxSurge: 1, maxUnavailable: 0` RollingUpdate keeps the old pod
serving until the new pod passes its readiness probe, and SSE
streams survive.

### Steps

1. **Add a `longhorn-rwx` StorageClass** (mirror of `longhorn`
   with `numberOfReplicas: "2"`, since Longhorn requires ≥2
   replicas for RWX). Already prototyped in
   `k8s/longhorn-rwx-sc.yaml`.

2. **Provision a new RWX PVC** populated from the live RWO
   volume. The plan was to use Longhorn's CSI volume clone
   (`spec.dataSource.kind: PersistentVolumeClaim` pointing at
   `tusker-home`). **This step is blocked** — see "What we
   tried" below.

3. **Switch deployment to RollingUpdate** in
   `k8s/deployment.yaml`:

   ```yaml
   strategy:
     type: RollingUpdate
     rollingUpdate:
       maxSurge: 1
       maxUnavailable: 0
   ```

4. **Mitigate SQLite-on-NFS write races.** Longhorn RWX uses
   an internal NFS server; multiple pods writing to the same
   SQLite file can corrupt it (NFS file locking is advisory,
   not mandatory). Add a one-time `PRAGMA journal_mode=WAL` on
   each DB at startup (`cooldowns.db`, `budget.db`, `circuit.db`,
   `ratelimit.db`, `cache.db`, `model_quality.db`). WAL mode
   lets concurrent readers proceed without blocking writers and
   serialises writers via fcntl locks (still NFS-weak, but the
   overlap window during a rollout is ~30s and writes are
   infrequent).

5. **Keep `auth.json` populated during the migration.** This is
   the load-bearing data — Codex OAuth credentials. If the
   gateway restarts with no `auth.json` and no
   `CODEX_CREDENTIALS` env var, every Codex model call fails
   until a human re-enrolls via device-code flow. **Verify
   `CODEX_CREDENTIALS` is sourced from the k8s secret before
   deleting the volume**, or copy `auth.json` back via the
   `tusker-init` init container pattern.

6. **Test with a real SSE stream during deploy.** The smoke
   test in `k8s/deploy.sh` only checks `/health` and `/ready`;
   it does not exercise `/v1/chat/completions` with `stream:
   true`. Add one before claiming this lands.

## What we tried (and why it didn't ship)

In August 2026 we attempted the live migration end-to-end:

- Created `longhorn-rwx` SC ✓
- Created `k8s/pvc-rwx.yaml` referencing `tusker-home` as
  `dataSource` for a full-copy clone ✓ (PVC bound, RWX access
  mode granted)
- The underlying Longhorn volume's `state` immediately went to
  `detaching` and stayed there indefinitely (10+ min). The
  Longhorn manager logs show a PV annotation concurrency
  collision (`the object has been modified`) and the clone
  attempt counter stuck at 1 with no retries.

The live service continued serving healthfully on the original
RWO PVC throughout — nothing was broken — but the migration
could not complete without a manual pod-stop to quiesce the
source volume's writes. We chose to roll back rather than cause
a deliberate outage.

## Risks

- **Storage cost**: RWX requires `numberOfReplicas: 2`, so the
  5Gi volume consumes ~10Gi of cluster storage and writes go to
  both replicas (2× write IO during normal operation).
- **NFS write races**: SQLite-on-NFS is fragile. WAL mode + low
  write rate + short overlap window makes this low-probability
  but non-zero. Acceptable for deploy-time, would be reckless
  for sustained two-pod operation.
- **Longhorn 1.12 clone bug**: we hit it; revisit after an engine
  upgrade.

## Decision

For now, document this as the intended path but **don't apply
the changes**. Future work:

1. Upgrade Longhorn engine (the `1.12.0` engine image is older
   than the active `1.11.2` engines, per Longhorn's auto-upgrade
   status; aligning might fix the clone bug).
2. Retry the clone after upgrade.
3. Land the deployment strategy change only after step 2
   succeeds in pre-prod / a quiescent window.