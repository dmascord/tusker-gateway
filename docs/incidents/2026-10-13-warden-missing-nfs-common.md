# warden missing `nfs-common` — Longhorn RWX mount failure (2026-10-13)

## Symptom

Deploying revision `f45b6ef` the scheduler placed the new gateway pod on
`warden`. The pod hung in `ContainerCreating` for ~18 minutes while
`kubectl rollout status` timed out. Production was never down: the previous
pod kept serving on `warlock` (`maxSurge: 1, maxUnavailable: 0`).

Pod events:

```
FailedAttachVolume  CSINode warden does not contain driver driver.longhorn.io   (transient)
SuccessfulAttachVolume                                                        (recovered)
FailedMount (x15)   mount failed: exit status 32
  Mounting command: /usr/local/sbin/nsmounter
  mount -t nfs -o vers=4.1,noresvport,timeo=600,retrans=5,softerr 10.97.82.3:/pvc-2df570ce...
  Output: fsconfig() failed: NFS: mount program didn't pass remote address
```

## Root cause

`warden`'s **host** had no NFS userspace. `nfs-common` was never installed on
that node (`dpkg -l nfs-common` → `un`, no `/sbin/mount.nfs`), while the other
nine nodes all had `nfs-common 1:2.8.3-1`. `nsmounter` enters the host mount
namespace and calls the host's `mount -t nfs`; without the `mount.nfs` helper
util-linux falls back to the raw `fsconfig()` path, which produces exactly
`NFS: mount program didn't pass remote address`.

The attach errors were red herrings: the Longhorn control plane was being
upgraded to v1.13.0 concurrently, so the CSI node plugin on `warden` restarted
under the mount attempt. The RWX volume itself stayed `attached/healthy`
throughout.

`tusker-home-rwx` is RWX, so the failure is not a single-writer conflict — it is
a node-capability gap. **Any** Longhorn RWX pod scheduled onto `warden` would
have hung the same way; earlier deploys simply happened to land on `warlock`.

## Fix

```bash
ssh 10.0.0.112 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-common'
```

Verified `mount.nfs: (linux nfs-utils 2.8.3)`, matching the other nodes. The
kubelet's next `MountDevice` retry succeeded, the pod became Ready, and the
rollout completed. No pod deletion, cordon, or manual reschedule was needed.

## Follow-ups

- `deploy.sh` aborts at its `rollout status` wait when a rollout is slow, so the
  later provenance-annotation step never runs. The annotations were replayed
  manually and `k8s/verify-provenance.sh` then reported OK. A slow rollout
  should not silently skip provenance recording.
- Node provisioning should install `nfs-common` on every Longhorn node; the
  gap survived `warden`'s join. A periodic check (the existing
  `disk-health-check` CronJob is a reasonable host) should assert
  `/sbin/mount.nfs` exists on every node.
- Longhorn control plane is now v1.13.0; volume engines are deliberately still
  v1.12.1 (`allow-instance-manager-automatic-upgrade: false`,
  `concurrent-automatic-engine-upgrade-per-node-limit: 0`). Engine upgrade is a
  separate, planned decision.
