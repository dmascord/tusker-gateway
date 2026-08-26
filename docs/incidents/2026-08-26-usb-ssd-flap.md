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

The `/var/lib/containerd` and Longhorn replica storage **both lived on a Samsung Portable SSD T5 connected via USB on `visor`**. The USB connection flapped three times in four seconds, ext4 aborted its journal on both LVs (which auto-remounted read-only), containerd crashed, kubelet went NotReady, and the RWO Longhorn replica on that single node became unattachable.

The disk is now stable (it was stable during the reboot and recovery), but **the underlying cause isn't fixed** — a consumer-grade USB SSD is still our storage. Recovery actions in this commit mitigate this; full removal of the USB SSD as runtime state is future work.

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

1. **Don't put containerd runtime state on a removable USB drive.** USB cables flap. ext4 doesn't survive mid-write USB drops cleanly.
2. **Don't put RWO Longhorn replica storage on a removable USB drive.** With `numberOfReplicas=1` (the Longhorn default) there's no failover. A USB flap on the only replica host = outage.
3. **Consumer-grade portable SSDs are not server-grade.** The T5 is meant for backup use, not sustained 24/7 I/O. Its write cache + USB connection combine badly.
4. **Watch `dmesg` for USB flap events before the kernel does.** We had no early warning when the disk started flapping. The new monitor catches flap cycles in <60s.

## Preventive follow-ups

1. **Move all critical state off the USB drive.** The USB T5 is still attached to visor (just no longer used for our critical data). When convenient, physically unplug it.
2. **Pre-deploy the engine image to every Longhorn node.** The replica #2 attempt stalled because wynk didn't have `docker.io/longhornio/longhorn-engine:v1.12.0` already pulled. Cluster-wide pre-pull would have made the bump work first try.
3. **Longhorn default `numberOfReplicas=1`.** We rely on the engine on the host the pod is on, with no redundancy. Setting all critical volumes to `numberOfReplicas=2` (or 3) removes the SPOF.
4. **Consider replacing the USB T5 with a proper SATA/NVMe SSD** if visor's internal storage is genuinely too tight. A 1 TB internal SSD is ~$80 and removes an entire class of failures.

## References

- Live USB event log: visor `dmesg | grep -E "usb|sdd|sdc|dm-"` around 2026-08-26T05:27
- The new monitor's source: `tusker_gateway/tools/usb-flap-monitor.{sh,service,timer}`
- The recovery commits: this git history from 2026-08-26 (translator registry, RTK shim, NVIDIA provider, rate-limit test fix, then this migration)
