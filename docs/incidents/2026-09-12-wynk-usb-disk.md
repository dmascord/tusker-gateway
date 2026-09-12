# Longhorn disk retirement: `usb-longhorn` on `wynk`, 2026-09-12

## Summary

The Longhorn disk entry `usb-longhorn` on node `wynk` had previously entered
retirement (`evictionRequested: true`, `allowScheduling: false`) after the USB
storage flap. Its disk-health checks reported a missing
`/mnt/kubelet/longhorn/longhorn-disk.cfg`. No Longhorn storage was scheduled on
the disk, so the safe repair was to remove the retired disk entry from the
Longhorn node specification. The filesystem was not reformatted and the node
was not deleted.

## Observed state before the change

- Node: `wynk`
- Longhorn disk: `usb-longhorn`
- Path: `/mnt/kubelet/longhorn`
- `spec.disks.usb-longhorn.allowScheduling`: `false`
- `spec.disks.usb-longhorn.evictionRequested`: `true`
- `status.diskStatus.usb-longhorn.storageScheduled`: `0`
- `status.diskStatus.usb-longhorn.scheduledReplica`: `{}`
- The default disk `/srv/data` was healthy and schedulable.

A fresh read immediately before the patch showed the USB disk status had
already recovered to `Ready=True` and `Schedulable=True`, but the spec still
retained the retired entry. Removing that stale entry prevents Longhorn and the
health checker from treating the retired USB path as an active managed disk.

## Action

At approximately `2026-09-12T07:47Z`:

```sh
kubectl -n longhorn-system patch nodes.longhorn.io wynk \
  --type=json \
  -p='[{"op":"remove","path":"/spec/disks/usb-longhorn"}]'
```

No filesystem operation, disk format, Longhorn node deletion, volume deletion,
or replica deletion was performed.

## Verification

The patched node reported:

- `spec.disks`: only `default-disk-f7f601c02e9bb38`
- `status.diskStatus`: only `default-disk-f7f601c02e9bb38`

The post-change `disk-health-verify` job started at `2026-09-12T07:47:20Z`
and completed successfully at `2026-09-12T07:47:40Z` with `succeeded=1`.
The scheduled `disk-health-check-29819985` job at `07:45Z` also completed
successfully.

## Result and follow-up

`wynk` continues to use its internal `/srv/data` Longhorn disk. The USB path is
no longer registered as a Longhorn disk and must not be re-added unless the
USB storage is intentionally restored, validated, and explicitly approved for
Longhorn use. The earlier USB-flap monitor remains useful for detecting any
future physical disturbance, but this retired disk no longer participates in
Longhorn scheduling.
