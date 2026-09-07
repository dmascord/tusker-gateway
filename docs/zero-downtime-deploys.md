# Zero-downtime deploys

Status: **shipped** (2026-09-07). The gateway runs `RollingUpdate`
with `maxSurge: 1, maxUnavailable: 0` on a shared RWX volume.
See "History" for the failed August 2026 attempts that delayed this.

## What shipped

1. **`longhorn-rwx` StorageClass** (`k8s/longhorn-rwx-sc.yaml`) —
   NFS-CSI backed RWX volumes.

2. **`tusker-home-rwx` PVC** (`k8s/pvc-rwx.yaml`) — ReadWriteMany,
   `dataSource`-cloned from the live RWO `tusker-home` volume so no
   state was lost or re-seeded.

3. **Deployment strategy** (`k8s/deployment.yaml`):

   ```yaml
   strategy:
     type: RollingUpdate
     rollingUpdate:
       maxSurge: 1
       maxUnavailable: 0
   ```

   plus `claimName: tusker-home` → `tusker-home-rwx`. The old pod
   keeps serving until the new pod passes readiness.

4. **SQLite WAL everywhere.** Every gateway SQLite store now sets
   `PRAGMA journal_mode=WAL` at connect time (commit `1171e5a`):
   budget, cache, circuit_breaker, model_capability,
   persistent_cooldown, provider_usage, quality, rate_limit,
   tool_capability (idempotency already had it). This tolerates the
   dual-writer window during a rollout on the NFS-backed RWX volume.

5. **SSE smoke test in `k8s/deploy.sh`.** After `/health` and
   `/ready`, the deploy script posts a streaming
   `/v1/chat/completions` request through the public ingress with a
   key from `tusker-env-vault` — proving end-to-end SSE delivery
   rather than just probe health. Non-fatal; log-only.

## Audience note on replicas

The deployment stays at `replicas: 1`. RWX + WAL makes the
**deploy-time** 2-pod coexistence safe, but sustained two-pod
operation keeps permanent dual-writer SQLite-on-NFS exposure
(fcntl locking over NFS is weak). Zero-downtime deploys don't
require steady-state redundancy. Chosen 2026-09-07.

## Validation evidence (2026-09-07)

- Rolling rollout on the live cluster: new pod healthy before the
  old pod began terminating; `maxUnavailable: 0` held throughout.
- Streaming `hermes-code` chat survived an in-flight
  `kubectl rollout restart`: 53,589 bytes emitted continuously,
  terminated with `data: [DONE]`, no dropped frames while the pod
  set swapped underneath it.
- Post-rollout smoke: `/health` 200, `/ready` pools intact
  (code: 241 configured / 226 selectable), `/v1/models` OK,
  authenticated chat round-trip OK.
- Local test suite: 764 passed, 2 skipped.

## Old RWO volume retention

The original `tusker-home` PVC (+ PV `pvc-2c78882c`) is **kept
intentionally** as an offline fall-back copy of pre-migration
state. Also kept: the stale `Released` PV `pvc-000956e5` from the
August attempt. Decision 2026-09-07: retain both; revisit deletion
only after the RWX clone has proven stable for an extended period.

## History

### The problem originally

The gateway deployment used `strategy.type: Recreate`: every
rollout killed the running pod before the new one started, dropping
every in-flight SSE stream. `Recreate` existed because the
`ReadWriteOnce` `tusker-home` volume prevented two pods from
mounting the same data, so `RollingUpdate` with `maxSurge: 1`
would stall forever.

Fixed once (commit `c2fbf9b`), reverted (`369e7f0`) when the PVC
constraint was noticed; shipped for real in September 2026 via the
RWX migration above.

### August 2026 attempt 1: direct PVC clone, default SC

`k8s/pvc-rwx.yaml` with `dataSource: tusker-home`. Longhorn took a
snapshot and began a full-copy clone; **the clone hung in
`state: detaching` indefinitely**. Live service stayed healthy;
manual cleanup forced the cluster to GC the stuck Longhorn volume.

### August 2026 attempt 2: diskSelector to exclude undersized disk

Tagged `wyrm`, `wytch`, `wyvern` disks with `rwx-capable`, added
`diskSelector` to `longhorn-rwx`, re-applied. Replica scheduled
cleanly on `wytch`; the clone engine was placed on `visor` — and
**crash-looped**: `no available backend ... tcp://192.168.162.116`.
It could not dial the replica on `wytch`.

### Root cause at the time: visor cross-node pod networking

Probes from a visor pod:

| Target | Result |
|---|---|
| `10.0.0.231` (visor host, same node) | OK |
| `8.8.8.8` (internet) | OK |
| `192.168.21.37` (visor pod, same node) | TCP refused (port not open) — networking fine |
| `192.168.239.211` (wyvern pod) | **Timeout** |
| `192.168.162.116` (wytch pod) | **Timeout** |
| Longhorn frontend ServiceIP | **Timeout** |

Reverse direction worked (wyvern → visor fine), so it was a
**visor-specific outbound cross-node failure** in the Calico/eBPF
path, independent of Longhorn.

### September 2026 resolution

At some point between the August attempt and 2026-09-07 the visor
cross-node networking fault cleared (the successful rollout on
2026-09-07 had the clone engine and gateway pods communicating
across nodes — the exact failure from August). The migration then
followed the documented steps and completed without incident:
clone engine finished, PVC bound, rolling rollout succeeded, SSE
stream continuity verified during a live restart.
