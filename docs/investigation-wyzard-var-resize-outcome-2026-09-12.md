# Wyzard /var resize — investigation outcome (2026-09-12)

## Summary

**Option A** (drain qdrant off wyzard): **DONE.** Qdrant now runs on visor.
**Option B** (shrink /home, grow /var): **NOT DONE.** Investigated and concluded
that an offline shrink would be required, which would drain wyzard of all 14
running pods including the tusker-gateway and PG. Per operator direction
(2026-09-12), the drain-and-shrink path was deferred pending alternative
investigation.

## Option A — drained qdrant to visor (DONE)

Applied `kubectl patch` to the `qdrant` Deployment in the `embed` namespace,
adding a hard nodeAntiAffinity excluding `wyzard`. The migration happened
end-to-end in **~30 seconds**:

- Longhorn volume state went `attached → detaching → attaching → attached`
  with `currentNodeID=visor` and `robustness=healthy`.
- New pod came up `Running` on visor at `192.168.21.41`.
- ClusterIP service `qdrant.embed.svc.cluster.local:6333` continues to
  resolve via kube-dns.
- `in-cluster healthz check passed`.

The 371 MB bind-mount that lived under `/var/lib/kubelet/pods/239833f2-.../`
on wyzard was reclaimed by kubelet's GC within ~2 minutes of the pod
terminating.

## Option B — resize attempt (BLOCKED, investigated alternatives)

### Initial plan

Shrink `wyzard--vg-home` from 32 GiB to 22 GiB (online shrink via
`resize2fs` then `lvreduce`). Grow `wyzard--vg-var` from 3.31 GiB to ~13 GiB
(via `lvextend` then `resize2fs`).

### Block 1: filesystem minimum size

`resize2fs -M` reports **6,798,232 blocks = 26 GiB** as the minimum
shrink target for the live `/home` filesystem. The 32 GiB filesystem
holds:
- 203 overlayfs container snapshots under
  `/home/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/`:
  **13 GiB**.
- Content store at `/home/containerd/io.containerd.content.v1.content/`
  (image layer blobs): 4.1 GiB.
- Other data (kubelet state, NAS dock, tusker/, jonah/, leighton/, dpkg
  metadata): ~9 GiB.
- **Minimum required for safe shrink: 26 GiB.**

The 22 GiB target was infeasible. **Revised plan: shrink to 26 GiB,
grow /var to ~9 GiB.**

### Block 2: ext4 online shrink not supported

`resize2fs /dev/mapper/wyzard--vg-home 26G` (or any smaller value) returns:

```
resize2fs 1.47.2 (1-Jan-2025)
/sbin/resize2fs: On-line shrinking not supported
Filesystem at /dev/mapper/wyzard--vg-home is mounted on /home; on-line resizing required
```

This is a Linux kernel constraint — ext4 online shrink has not been merged
upstream as of 6.12. The only path forward is offline shrink, which
requires the filesystem to be unmounted, which requires the
`wyzard--vg-home` LV to be free of live bind-mounts.

### Block 3: live bind-mounts on /home

`/home` is heavily used by kubelet for active pods:

```
/home/kubelet/pods/<uid>/volume-subpaths/host-nft-wrapper/calico-node/9
/home/kubelet/pods/<uid>/volume-subpaths/config/mosquitto/0
...
```

Live unmount of `/home` would break the Longhorn instance-manager, the
calico-node pods, the mqtt deployment, etc. **The only safe path is
`kubectl drain wyzard`**, which evicts all 14 running pods (gateway, PG,
mosquitto, vault, calico-node, kube-proxy, coredns, nfd-worker, longhorn
manager/csi-plugin, etc.) before the filesystem can be taken offline.

### Alternative paths investigated (per operator direction)

The operator asked for alternatives. The paths I evaluated:

1. **Manual containerd snapshot GC.**
   - 161 snapshots with status `Committed` looked orphan-like, but the
     snapshot tree shows they're all part of active pod sandbox + container
     chains ending in live container IDs. `ctr snapshots tree` confirms
     chains like `pause image → sandbox layer → application layer → active
     container`.
   - Removing them would orphan the active container's filesystem layer.
     Not safe without first evicting the pods.
2. **Shrink wyzard--vg-docker (190 GiB LV, 172 GiB used).**
   - Holds 4 local Docker volumes (`frigate_storage-volume`, `mc-data`,
     `double-take_double-take`, `compreface_postgres-data`). Docker LV
     shrink would risk data loss in those volumes. Not safe without
     first draining the docker-managed pods.
3. **Add a new physical disk to wyzard's VG.**
   - Requires hardware intervention (seat a new disk on the host).
     Out of scope for a configuration change.
4. **Move additional workloads off wyzard.**
   - Like Option A, for the gateway and PG. Smaller individual savings
     (gateway uses RWX PVC, not local `/var`; PG uses a 10 GiB Longhorn
     PVC). Wins a few MB but doesn't solve the structural constraint.

None of these provide a non-disruptive shrink of `/var`.

### Final state

- **`/var` LV remains 3.31 GiB, 99% used.** Real content ~218 MB
  (per Python `os.lstat` walk); the rest (~2.8 GB) is ext4 metadata
  (journal metadata, reserved blocks, inode table, block group
  descriptors). Wyzard has 960 MB free in the VG.
- **Functional pressure relief:** Option A drained qdrant (was the main
  content contributor on `/var` at ~371 MB; now reclaimed). The PV
  snapshot blob directory still exists but only as detached
  CSI artifacts, cleaned up automatically by kubelet GC within ~2 minutes.
- **Persistent pressure:** without a node drain, `/var` cannot grow.
  Image pulls (>600 MB each for gateway image pulls) and qdrant WAL
  expansion (if qdrant ever reschedules here) will continue to tip the
  LV past its threshold.

## Recommendation (carrying forward)

1. **Schedule a maintenance window to drain wyzard and shrink /home.**
   - Workflow: `kubectl drain wyzard --ignore-daemonsets --delete-emptydir-data`
   - Wait for all 14 pods to evict (grace period per pod).
   - `umount /home` (need to also unmount the bind-mounts first).
   - `resize2fs /dev/mapper/wyzard--vg-home 26G`
   - `lvreduce wyzard-vg/home -L 26G`
   - `lvextend wyzard-vg/var -l +100%FREE`
   - `resize2fs /dev/mapper/wyzard--vg-var`
   - `mount /home`
   - `kubectl uncordon wyzard` (pods reschedule automatically).
2. **In the same window, enable kubelet container GC** to avoid future
   snapshot drift:
   - Add `maximum-dead-containers: 10` and
     `maximum-dead-containers-per-container: 2` to
     `/var/lib/kubelet/config.yaml` on each worker node.
   - Restart kubelet (`systemctl restart kubelet`).
3. **Set up monitoring** for `/var` LV usage so we get a 50% threshold
   alert before the next ENOSPC event.

## Files

- `docs/embed-namespace-snapshots-2026-09-12/qdrant-deployment-before-anti-affinity.yaml`
  — the qdrant deployment YAML captured before the affinity edit, kept
  for the record.
- `docs/investigation-wyzard-var-2026-09-12.md` — root-cause report.
- `docs/investigation-wyzard-var-resize-plan.md` — original Option B plan
  (predates the live investigation; left for traceability).
