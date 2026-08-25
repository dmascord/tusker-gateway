# Zero-downtime deploys

Status: **designed but not yet implemented**. See "What we tried" for the
concrete blocker discovered in August 2026.

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
   replicas for RWX). Already prototyped and applied to the
   cluster during the August 2026 attempt.

2. **Provision a new RWX PVC** populated from the live RWO
   volume, using Longhorn's CSI volume clone
   (`spec.dataSource.kind: PersistentVolumeClaim` pointing at
   `tusker-home`).

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

## What we tried (August 2026, two attempts)

### Attempt 1: direct PVC clone, default SC

First attempt created `k8s/pvc-rwx.yaml` referring to
`tusker-home` as `dataSource`. Longhorn created a new volume,
took a snapshot of the source, and began a full-copy clone.
**The clone hung in `state: detaching` indefinitely** —
visible in `kubectl get volumes.longhorn.io` and engine logs.
Live service stayed healthy; manual cleanup forced the
cluster to GC the stuck Longhorn volume.

### Attempt 2: diskSelector to exclude undersized disk

Second attempt tagged `wyrm`, `wytch`, `wyvern` disks with
`rwx-capable`, added `diskSelector: rwx-capable` to the
`longhorn-rwx` SC, and re-applied the PVC. The replica
correctly scheduled on `wytch` (no longer on the undersized
`wynk` disk — that was the original failure). The replica
came up cleanly on `wytch`. The clone engine was placed on
`visor` by Longhorn's volume-clone-controller.

Then: **the engine crashed in a loop with**
`no available backend due to service unavailable from the
addresses [tcp://192.168.162.116:PORT]` — it could not dial
the replica on `wytch`.

### Root cause: visor's pod-to-pod networking is broken

Direct connectivity probes from a pod running on `visor`:

| Target | Result |
|---|---|
| `10.0.0.231` (visor host, same node) | OK |
| `8.8.8.8` (internet) | OK |
| `192.168.21.37` (another visor pod, same node) | Refused (port not open) — TCP works |
| `192.168.239.211` (wyvern pod, gw) | **Timeout** |
| `192.168.162.116` (wytch pod) | **Timeout** |
| `10.97.174.68:80` (longhorn-frontend ServiceIP) | **Timeout** |

And the reverse direction is fine — a wyvern pod can reach
visor pods. So it's a **visor-specific outbound cross-node
pod traffic failure**. Visor's hosting table looks correct
(there's a `192.168.162.64/26 via 10.0.0.120` route for the
wytch subnet), but packets from visor pods to non-visor
pods don't get through.

This is independent of Longhorn — it's a Calico / visor
node network misconfiguration that affects any pod
attempting outbound cross-node traffic from visor. Likely
candidates (none confirmed without cluster admin):

- eBPF dataplane stale program on visor's calico-node
- An iptables `KUBE-FORWARD` rule that's been overwritten
- A stale conntrack entry from an older pod IP

Because the engine keeps landing on visor (Longhorn sets
`spec.ownerID` to visor for clone volumes and the engine
follows the owner), and visor pods can't reach wytch pods,
the engine crash-loops indefinitely.

### Live state after the failed attempt

- Original `tusker-home` (RWO) untouched, healthy on wyvern.
- The `longhorn-rwx` SC was deleted after the failure to
  avoid confusing future deploys.
- No PVC references RWX; no Longhorn volumes left over from
  the clones.
- Live `/health` returns 200 on `https://ai.tusker.net.au/`.

### Cluster-side remediation (out of repo scope)

Before this migration can succeed, **the cluster admin
needs to fix visor's cross-node pod-to-pod networking**.
Symptoms to look for:

- A pod on `visor` cannot `curl` or `nc` to a pod on `wyrm`,
  `wytch`, or `wyvern` — but pods on those nodes can reach
  visor pods.
- `kubectl exec` into any visor pod and run
  `timeout 3 nc -zv <pod-ip-on-other-node> <port>` to
  reproduce.
- Compare visor's `ip rule` and Calico's eBPF state against a
  healthy node (e.g. wyrm).

If that gets fixed, attempt 2's setup (the diskSelector SC
+ tagged healthy disks) should work — the replica and engine
will land on nodes that can reach each other.

## Risks (post-fix)

- **Storage cost**: RWX requires `numberOfReplicas: 2`, so the
  5Gi volume consumes ~10Gi of cluster storage and writes go to
  both replicas (2× write IO during normal operation).
- **NFS write races**: SQLite-on-NFS is fragile. WAL mode + low
  write rate + short overlap window makes this low-probability
  but non-zero. Acceptable for deploy-time, would be reckless
  for sustained two-pod operation.
- **Longhorn full-copy clone is slow** (full 5Gi byte-for-byte
  even when actual usage is much smaller). On a healthy cluster
  it should complete in 1-5 min; the August 2026 attempt never
  got to measure this because the engine couldn't reach the
  replica.

## Decision

Documented as the intended path but **don't apply the
deployment strategy change** until the visor's cross-node
networking is repaired cluster-side. Live service has been
healthful on the original RWO PVC throughout; the migration
risk is not worth breaking that to chase zero-downtime
deploys.

When the cluster is repaired:

1. Re-apply the SC and disk tags from attempt 2 (see
   `k8s/pvc-rwx.yaml` and `k8s/longhorn-rwx-sc.yaml` from
   the working tree).
2. Run the clone. Verify the engine lands on a non-visor
   node and the replica on a tagged disk.
3. Switch the deployment to RollingUpdate.
4. Add the SQLite WAL mode init step (step 4 above).
5. Test with an actual SSE stream during a deploy before
   claiming success.
