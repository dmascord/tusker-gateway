# Incident postmortem: USB-SSD flap, 2026-08-26

## Summary

At ~05:27 UTC on 2026-08-26, the Samsung Portable SSD T5 attached to visor's USB bus flapped three times in four seconds. ext4 aborted its journal on both LVs (containerd-data, longhorn-ssd), the kernel remounted them read-only, containerd crashed, kubelet went NotReady, and the only Longhorn replica for tusker-home became unattachable. Every `/v1/chat/completions` returned 500 for ~1 hour until we rebooted visor.

## Timeline (UTC)

| Time | Event |
|------|-------|
| ~05:27 | Samsung T5 USB drive flaps 3x in 4 seconds (USB disconnect → new device → disconnect → new device → disconnect → new device) |
| ~05:27 | Longhorn replica on visor reports bitmap errors; ext4 on containerd-data aborts journal and remounts read-only |
| ~05:27 | containerd crashes (can't write state); kubelet → NotReady; pod evicted |
| ~05:30+ | New pod on wyvern mounts the broken PVC; every SQLite open returns I/O error → 500s |
| ~05:40 | First alerts land in the gateway logs (visible to operators) |
| ~06:23 | I started investigating |
| ~06:24 | Tried fsck in-place; blocked by jbd2/dm-5 holding the LV |
| ~06:25 | Rebooted visor to release jbd2 |
| ~06:25 | visor came back; fsck ran automatically (Filesystem state: clean) |
| ~06:26 | Longhorn auto-salvaged the broken replica |
| ~06:27 | Longhorn attached the rebuilt replica on wyvern |
| ~06:28 | Pod rescheduled onto wyvern where the new engine + replica live |
| ~06:28 | Chat endpoint returns 200 with a real response |

## Root cause

The `/var/lib/containerd` and Longhorn replica storage **both lived on a Samsung Portable SSD T5 connected via USB on `visor`**. The USB connection flapped three times in four seconds — most likely from a physical bump of the USB cable or the drive itself (operator confirmed post-incident: the drive only blips when physically disturbed; otherwise it has been stable for years). ext4 aborted its journal on both LVs (which auto-remounted read-only), containerd crashed, kubelet went NotReady, and the RWO Longhorn replica on that single node became unattachable.

**Caveat**: the USB drive is not actively failing on its own — it was disturbed. The postmortem actions below protect against the next disturbance, not against an underlying hardware fault. The proper long-term fix is to physically unplug the drive from visor's USB bus (currently still attached but no longer used for critical state).

## Storage topology — before vs after

### Before (this incident)

visor physical disks:

| Disk | Type | Mount | Used by |
|------|------|-------|---------|
| sda (111 GB) | internal SSD | /srv/opencode | opencode data |
| sdb (476 GB) | internal SSD | visor-vg (root, var, home, tmp, swap) | OS + container home |
| sdc (931 GB) | **Samsung USB T5** ⚠ | ssd-vg (containerd-data, longhorn-ssd) | containerd runtime + Longhorn replicas |
| sdd (10 GB) | iSCSI | ephemeral pod mount | (unused) |

The `containerd-data` and `longhorn-ssd` LVs were both on the USB drive. A USB flap took both down simultaneously.

### After (recovery actions taken)

- **containerd-data removed from USB** — moved to `/var/lib/containerd` on visor's internal SSD (`visor-vg/var`). No longer a USB-affected runtime.
- **Longhorn's `longhorn-ssd` disk disabled for scheduling** — `allowScheduling: false`. New replicas won't go on the USB drive. Existing replicas on it (other apps' PVCs) untouched.
- **USB-flap monitor deployed** — `tusker_gateway/tools/usb-flap-monitor.{sh,service,timer}` runs every minute on visor via systemd timer. Outputs `ALERT usb-flap-detected ...` to journal on each flap.
- **Replica count for `tusker-home` bumped to 2 — *deferred***. During the immediate recovery the Longhorn admission webhook timed out twice when we tried to patch `numberOfReplicas=2`. We reverted to 1 to avoid blocking the gateway. To retry:

  ```bash
  kubectl -n longhorn-system patch volume pvc-000956e5-fd19-492d-b72f-5a21776c0ba8 \
    --type=merge -p '{"spec":{"numberOfReplicas":2}}'
  ```

## Lessons

1. **Don't put containerd runtime state on a removable USB drive.** Even if the drive is "usually stable," a single USB bump takes down the node. ext4 doesn't survive mid-write USB drops cleanly.
2. **Don't put RWO Longhorn replica storage on a removable USB drive.** With `numberOfReplicas=1` (the Longhorn default) there's no failover. A USB flap on the only replica host = outage.
3. **The USB T5 itself is not at fault.** It was disturbed. The hardware is fine; the topology was fragile. Fixing the topology (removing the USB drive from the critical path) is the durable fix.
4. **Watch `dmesg` for USB flap events before the kernel does.** Even if the flap is "operator-caused", an early alert lets us check that the node is still healthy and that the drive's mount is back.

## Preventive follow-ups

1. **Physically unplug the USB T5.** Since the drive is fine when left alone, simply removing the cable ends the fragility without losing data. The two non-tusker PVCs (`pvc-2afe302d`, `pvc-84387ebb`) on `longhorn-ssd` would need to be migrated off first; talk to the other app owners.
2. **Pre-deploy the engine image to every Longhorn node.** The replica #2 attempt stalled because wynk didn't have `docker.io/longhornio/longhorn-engine:v1.12.0` already pulled. Cluster-wide pre-pull would have made the bump work first try. *(Note: when re-attempted on 2026-08-27, the image *was* on wynk, so this was no longer the blocker — the stuck replica is now a Longhorn controller bookkeeping issue.)*
3. **Longhorn default `numberOfReplicas=1`.** We rely on the engine on the host the pod is on, with no redundancy. Setting all critical volumes to `numberOfReplicas=2` (or 3) removes the SPOF. *Re-attempted on 2026-08-27 and a stuck replica was created on wynk that the controllers won't reconcile — see the postmortem addendum below.*
4. **Consider a proper internal SSD replacement** if visor's internal storage is genuinely too tight for a fresh install. A 1 TB SATA SSD is ~$80 and removes an entire class of failures — but this is **optional** given that the drive is fine when untouched.

## Postmortem addendum (2026-08-27)

Re-verified the diagnosis with the operator: the Samsung T5 only flaps when **physically touched** (cable bump, drive shift). It has been stable for years otherwise. This means the postmortem actions are sufficient protection — the USB drive no longer carries critical state, and the floppy cable is just one less thing in the critical path.

Attempted to bump `tusker-home` to `numberOfReplicas=2` on 2026-08-27. The patch landed, but Longhorn created a replica on wynk that the controller would not reconcile (`stopped` for >17 hours, `Current Image: ""`, `Started: false`, not in the engine's `replicaAddressMap`). Annotation/patch/delete cycles don't transition the replica out of `stopped`. The single-replica configuration remains in place; gateway traffic is unaffected.

When retrying this, the right path is to first do a Longhorn version-aligned snapshot and restore cycle on a fresh volume, OR accept that bumping replicas on this cluster requires Longhorn version upgrades that fix the controller reconciliation bug. Until then, the volume runs healthy on a single replica on visor's internal `var-longhorn` disk.

The USB T5 is still attached to visor but no longer used for tusker-gateway data. When the other-app PVCs on it (`pvc-2afe302d`, `pvc-84387ebb`) are migrated off, the drive can be physically unplugged and the USB-flap monitor can be removed.

## Longhorn v1.12.1 upgrade (2026-08-27)

Upgraded Longhorn from chart `1.12.0` to `1.12.1` (app version `v1.12.0` → `v1.12.1`) via `helm upgrade longhorn longhorn/longhorn --version 1.12.1`. The cluster went through Helm chart revision 2 → 4 (revision 3 was a no-op because `--reuse-values` carried the previously computed image tag `v1.12.0` forward; the explicit `--set image.*.tag=v1.12.1` overrides were required).

The upgrade delivered two concrete benefits:

1. **Fixes the kernel-6.12+ ext4 read-only detection bug** ([Issue #13482](https://github.com/longhorn/longhorn/issues/13482)) that caused today's incident. ext4 ≥ 6.12 reports `emergency_ro` rather than `ro`, and pre-v1.12.1 Longhorn didn't check for that flag — so the read-only filesystem from the USB flap was invisible to Longhorn, the replica wasn't auto-salvaged, and the volume stayed `degraded` until operator intervention. **This fix is the durable solution to today's outage class.**

2. **Cleans up the stuck replica and accepts `numberOfReplicas=2`.** On v1.12.1, bumping `tusker-home` to `numberOfReplicas=2` succeeded within ~3 minutes (replica #2 came up on `wytch` in 15 seconds). On v1.12.0 it sat in `stopped` for 17+ hours with no reconciliation.

### Operational notes from the upgrade

- **`--reuse-values` semantics**: Helm's `--reuse-values` carries forward the **computed values** from the previous release, including chart defaults that were rendered. To override chart defaults (like image tags), pass explicit `--set` or use a values file.
- **NetworkPolicy defaults**: v1.12.1 enables internal NetworkPolicies by default. We explicitly disabled this with `--set networkPolicies.enabled=false` because (a) we don't run Prometheus/ServiceMonitor scrapers so there's no immediate benefit, and (b) our Calico CNI setup with BGP routing may need separate tuning for the policies to work cleanly. This is a future hardening step, separate from the FilesystemReadOnly fix that mattered here.
- **Wynk storage constraint**: One manager pod couldn't schedule on `wynk` because `wynk` has only 6.7 GiB of ephemeral-storage capacity (vs the manager pod's `/boot` HostPath volume requirement). This is a pre-existing constraint on the edge node and didn't cause issues — 5 of 6 managers running is sufficient for HA.

### Final post-upgrade state

- Helm chart: `longhorn-1.12.1`, app version `v1.12.1`
- 5/6 Longhorn managers running (visor, wyrm, wyzard, wytch, wyvern — wynk excluded by taint/storage)
- All CSI components (`csi-attacher`, `csi-provisioner`, `csi-resizer`, `csi-snapshotter`) and `longhorn-ui`, `longhorn-driver-deployer` running on v1.12.1
- Engine image `ei-493e04e7 (v1.12.1)` deployed; replicas still running on `v1.12.0` engine image but can be upgraded with `concurrentAutomaticEngineUpgradePerNodeLimit` setting when ready
- `tusker-home`: `numReplicas=2`, replicas running on `visor` and `wytch`, robust, healthy
- Pre-upgrade safety-net snapshot `pre-upgrade-1.12.1-1787791143` retained as a rollback reference (size 81.8 MB)
- S3 backup `backup-pre-upgrade-1.12.1-1787791169` completed (in case the on-cluster snapshot is also affected by future disk failures)

## References

- Live USB event log: visor `dmesg | grep -E "usb|sdd|sdc|dm-"` around 2026-08-26T05:27
- The new monitor's source: `tusker_gateway/tools/usb-flap-monitor.{sh,service,timer}`
- The recovery commits: this git history from 2026-08-26 (translator registry, RTK shim, NVIDIA provider, rate-limit test fix, then this migration)
