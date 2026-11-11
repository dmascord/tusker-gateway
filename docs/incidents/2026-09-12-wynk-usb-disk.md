# Longhorn disk retirement: `usb-longhorn` on `wynk`, 2026-09-12

## Summary

`/dev/sdb1`, the ASM246X-enclosed Samsung 990 EVO Plus 2 TB attached to `wynk`,
had been flapping repeatedly. The Longhorn disk was retired after the 2026-08
USB-SSD incident and removed from the node spec on 2026-09-12. The path was
then verified clean and re-registered as `usb-longhorn` with
`allowScheduling: false`. A soak period is required before any replica
scheduling is enabled. The filesystem was not reformatted and the node was not
deleted.

## Observed state before the change

- Node: `wynk`
- Longhorn disk: `usb-longhorn`
- Path: `/mnt/kubelet/longhorn`
- `spec.disks.usb-longhorn.allowScheduling`: `false`
- `spec.disks.usb-longhorn.evictionRequested`: `true`
- `status.diskStatus.usb-longhorn.storageScheduled`: `0`
- `status.diskStatus.usb-longhorn.scheduledReplica`: `{}`
- The default disk `/srv/data` was healthy and schedulable.

A fresh read immediately before re-registration showed the USB disk status had
recovered to `Ready=True` and `Schedulable=True`. The host checks showed
`/dev/sdb1` mounted read-write at `/mnt/kubelet`, ext4 state `clean`, and
`/mnt/kubelet/longhorn/longhorn-disk.cfg` present with `state: "ready"`.

The node's USB recovery agent was also found to be installed but crashing on a
missing `ESCALATION_TIMEOUT` constant. That source defect was fixed, deployed,
and the service returned to `MONITORING`.

## Initial retirement action

At approximately `2026-09-12T07:47Z`, the stale retired entry was removed:

```sh
kubectl -n longhorn-system patch nodes.longhorn.io wynk \
  --type=json \
  -p='[{"op":"remove","path":"/spec/disks/usb-longhorn"}]'
```

No filesystem operation, disk format, Longhorn node deletion, volume deletion,
or replica deletion was performed.

## Re-registration and monitoring

At approximately `2026-09-12T07:59Z`, the disk was re-registered with
`allowScheduling=false` and `evictionRequested=false`. Longhorn reported the
disk `Ready=True`, with a fresh heartbeat and `storageScheduled=0`.

The recovery agent source was updated in
`/Volumes/dev/dev/k8s/scripts/maintenance/usb-disk-recovery-agent.py`:

- fixed missing `POLL_INTERVAL` and `ESCALATION_TIMEOUT` definitions;
- made recovery preserve the scheduling gate, defaulting to unschedulable;
- deployed the fix to `wynk` and verified the systemd service is `active`;
- configured `/etc/usb-disk-recovery/config.json` with
  `allow_scheduling_after_recovery: false`.

The existing `disk-health-check` job passed after re-registration. Its output
also showed unrelated Kubernetes API/node-readiness query errors (`DNS` and an
uninitialized `nodes` variable) while still printing `All checks passed`; those
checker defects should be fixed before treating it as a reliable alert.

## Current result and scheduling policy

The disk remains registered for health observation and replica placement. The
operator policy permits scheduling when the recorded SMART evidence is healthy
and Longhorn reports the disk `Ready=True`. During the 2026-11-06 Longhorn
incident investigation, live evidence met that bar: the Samsung 990 EVO Plus
reported `PASSED`, 0% used, zero media/data-integrity errors, and zero
error-log entries; Longhorn reported `Ready=True`. The live gate was therefore
confirmed as `allowScheduling=true` with `evictionRequested=false`.

Existing replicas were intentionally left in place; no filesystem operation,
replica eviction, rebuild, or deletion was performed. The disk-health CronJob
does not prove full Longhorn data-path integrity, and the current degraded
volume investigation remains separate. Continue wynk-local monitoring for USB
disconnects and ext4/JBD2 I/O errors; disable scheduling if those checks fail.
