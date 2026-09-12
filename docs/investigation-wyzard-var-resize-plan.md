# Resize wyzard /var LV (Option B)

## Goal

Wyzard's `/var` is on a 3.2 GiB LV that's structurally too small for a k8s
worker running Longhorn (135 MB engine binaries), qdrant (300-400 MB), the
gateway pod, the dedicated PG pod, mosquitto, vault, coredns, kube-proxy,
calico, etc.

After Option A (drain qdrant off wyzard), the actual *content* on `/var`
is approximately 220 MB. The remaining "used" space reported by `df`
(~2.8 GB) is filesystem overhead: ext4 reserved blocks (5 % of LV =
~163 MB), the journal metadata, inode table, and block group descriptors.
On a small LV that overhead is a disproportionately large fraction of the
total.

## Plan

1. Verify qdrant pod is on `visor` and reachable through the in-cluster
   `qdrant.embed.svc.cluster.local` Service.
2. Drain wyzard safely: respect pods still scheduled there
   (tusker-gateway, mosquitto, vault, PG, calico, kubelet plugins,
   Longhorn instance manager). Direct footprint is small for those
   (siege-few MB on `/var`). So no drain needed.
3. Shrink `/home` LV (`wyzard--vg-home`) from 32 GiB to 22 GiB on the live
   mounted filesystem. This requires `lvreduce` then `resize2fs`.
4. Extend `/var` LV (`wyzard--vg-var`) from 3.31 GiB to 13 GiB on the live
   mounted filesystem. This requires `lvextend` then `resize2fs`.
5. Verify: `df -h /var` shows the new size; pod scheduling still works.

## Risks and mitigations

- **Destructive LVM ops on a production node.** Same risk surface as any
  in-place filesystem resize. Mitigations:
  - Snapshot the gateway pod's RWX PVC state via Longhorn snapshot
    (well-known, reversible, instant).
  - Keep a TTY session reserved so I can issue `pvresize --test` /
    `lvreduce --test` first where the tool allows.
  - Verify each step with `df -h` and `lvs` before proceeding.
- **Online ext4 shrink can corrupt the filesystem if the source volume is
  larger than the target size after the shrink (i.e. blocks past the new
  boundary are in use).** Mitigations:
  - `resize2fs` step BEFORE `lvreduce` to shrink the filesystem first.
  - Verify with `e2fsck -f -n` that filesystem is clean.
  - Maintain `lvreduce` failure-detection by doing the steps in 5 GB
    increments, not all-at-once.
- **`/home` in active use.** Many pod sandboxes bind-mount over
  `/home/kubelet/pods/<uid>/volume-subpaths/...`. An interrupted shrink
  could drop a pod's mountpoint and break its operation temporarily.
  Mitigations:
  - Stop/pause writable deployments while shrinking.
  - In practice, an unmounted `ext4` resize is safe; live resize is
    risk-bounded.
