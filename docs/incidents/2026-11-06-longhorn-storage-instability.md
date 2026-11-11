# Longhorn storage instability affecting the internal registry (2026-11-06)

## Status

Investigation and remediation in progress. The internal registry is currently
serving on a running pod, but its Longhorn volume remains `attached/degraded`
with one `RW` replica and one rebuilding `WO` replica. The gateway Service is
still temporarily routed to the config canary; both gateway pods currently
answer local `/health` and `/ready` checks, but the production pod has had
health-check timeouts and 9 restarts during this incident. Do not treat this
as storage recovery.

## User-visible impact

The registry Service temporarily had no endpoints. Gateway pods could not pull
the pinned gateway image and entered `ImagePullBackOff`, contributing to
gateway 503s. When the registry pod was running, it served its HTTP endpoint;
the container filesystem was about half full, so registry filesystem capacity
was not the cause observed during the initial check.

## Current observations (2026-11-06)

- Registry PVC `registry/registry-data` is bound to Longhorn volume
  `pvc-b40bcefc-8144-4b0b-a92f-06e2392fbcb5` (150 GiB, RWO, two replicas).
- At 05:26 UTC Longhorn auto-salvaged a replica. The volume attached on
  `wraith`, but at 05:28 UTC became degraded again while rebuild activity
  continued. The current replica resources report `running` on `wynk` and
  `wanderer`; that status alone does not establish replica consistency or
  volume health.
- At initial capture, three other attached volumes also reported `degraded`:
  two 20 GiB volumes on `wraith` and a 50 GiB volume on `visor`. Their
  degradation/auto-salvage timestamps clustered around 04:47–05:31 UTC, so
  the initial symptom was broader than the registry volume. They subsequently
  returned to `healthy`; see the timestamped recovery evidence below.
- Longhorn events show failed engine/replica RPCs, connection refused, EOF,
  read/write timeouts, and failed replica rebuilds. Instance-manager output
  contains replica timeouts and engine `no backend available` I/O errors.
- Visor's host kernel log provides a direct data-path failure at 2026-11-06
  16:19 local time (05:19 UTC): the 150 GiB iSCSI device attached for the
  registry reported SCSI `Medium Error / Unrecovered read error` on multiple
  sectors. EXT4 directory/inode reads failed, the journal aborted, the
  filesystem shut down, and the iSCSI device reset. Earlier, at 15:47 local,
  the same node logged unrecovered reads and EXT4 journal failure on a
  different Longhorn block device while `postgres` was accessing it. This
  places the incident below the registry application/filesystem layer; it does
  not yet identify which backing replica or node-side I/O path returned the
  medium errors.
- The registry Longhorn backup volume is present and last reports backup
  `backup-ae49097b9b8f42b7` completed at 2026-11-01 03:15 UTC. This is a
  recovery point, not a backup of the post-incident state. Preserve it while
  checking replica consistency.
- Host SMART access to the `wynk` USB-backed Longhorn disk succeeded using the
  ASMedia NVMe bridge mode: Samsung 990 EVO Plus 2 TB, health `PASSED`, 0%
  percentage used, 0 media/data-integrity errors, 0 error-log entries, 54 C.
  No kernel USB/UAS resets or media errors were found on `wynk` in the
  incident window. This reduces the evidence for a failing physical USB SSD,
  but does not validate the filesystem or the other replica hosts.
- At 05:44 UTC the registry pod became `1/1 Running` and its Service had an
  endpoint again. The Longhorn volume remained degraded. The engine's
  `replicaModeMap` showed only one `RW` replica (`r-e99c432d` on `wanderer`);
  the replacement replica was not yet in that map. Public gateway `/health`
  returned 200 through the canary (commit `e53c35ca`, config generation 50),
  not through production. Current verification is recorded below.
- The 10/13 Warden incident doc said engines were deliberately on v1.12.1, but
  live state now has only EngineImage v1.13.0 deployed (refcount 174), and
  degraded volumes use v1.13.0. This is configuration/documentation drift;
  its change history and intended engine-upgrade decision are not yet known.
- Applied a mitigation at 05:44 UTC: Longhorn setting
  `fast-replica-rebuild-enabled` changed from `{"v1":"true","v2":"true"}`
  to `{"v1":"false","v2":"true"}` (`applied=true`). The V1 fast-rebuild
  integrity issue [#14335](https://github.com/longhorn/longhorn/issues/14335)
  was reported after v1.13.0 shipped: purge/coalesce can change a snapshot
  while fast rebuild trusts a stale checksum, leaving missing data. This is a
  precaution, not a confirmed root cause. It makes V1 rebuilds slower and does
  not repair an existing replica or alter the currently attached RW replica.
- The live `wynk` Longhorn node had drifted from the documented scheduling state: `usb-longhorn.allowScheduling` was `true`, and its status listed existing replicas, including the registry replica. The disk reported `Ready=True`; this is not by itself proof that the USB-backed data path is safe.
- At 2026-11-06 investigation time, `usb-longhorn.allowScheduling` was reviewed against the current operator policy and restored to `true`; `evictionRequested=false` remained unchanged. The decision is based on the recorded SMART evidence for the Samsung 990 EVO Plus (health `PASSED`, 0% used, zero media/data-integrity errors, zero error-log entries) plus the current Longhorn `Ready=True` state. This permits scheduling and does not evict, delete, or rebuild any existing replica.
- The `visor/longhorn-ssd` disk was reviewed under the same policy and restored to `allowScheduling=true`. Its recorded SMART evidence reported `PASSED` with zero reallocated, uncorrectable, or CRC errors; Longhorn currently reports the disk `Ready=True`. The disk has one existing Hindsight replica; no volume or replica mutation was performed.
- Direct live inspection mapped the other initially degraded volumes:
  `pvc-349b4b96-4e3d-4119-9dee-7f16092cc5b2` is
  `wazuh/wazuh-manager-worker-wazuh-manager-worker-0`,
  `pvc-4b69022e-f1a4-4d39-bf64-2a3bc4f841e4` is
  `wazuh/wazuh-manager-master-wazuh-manager-master-0`, and
  `pvc-f6c33cc3-2fc7-42f1-b4af-2c4a15ea8a55` is `hindsight/hindsight-data`.
  They are now healthy. No replica, volume, PVC, or snapshot was deleted.
- Host SSH names for the storage nodes do not resolve from this workstation; the documented external SSH path to visor timed out. Host kernel logs and direct disk SMART/I/O checks are therefore still incomplete for the affected node-local paths.

## Work order

1. **Registry volume (`pvc-b40...`)** — preserve its data and backup lineage;
   identify which backing replica/path is returning SCSI medium errors, wait
   for both replicas to report healthy, then verify registry reads and image
   pulls.
2. **Other three degraded attached volumes** — map each PVC to its workload and
   owner, collect replica/engine evidence, and stabilize each independently.
3. **Shared Longhorn/node/storage path** — correlate all four volumes' event
   times, replica hosts, disk IDs, instance-manager errors, host kernel logs,
   disk capacity and health. Check the USB-backed `wynk` disk and the node-local
   data path on `wraith`, `wisp`, `wanderer`, `visor`, and `wyrm` as applicable.
4. **Version/config drift** — establish why control plane and all 174 engine
   references are v1.13.0 while `latest-longhorn-version` is v1.12.1 and the
   10/13 incident report says engines stayed on v1.12.1. Upstream resilience
   fixes are being backported into the upcoming v1.12.2 line; do not attempt a
   downgrade from v1.13.0. Reconcile only after confirming the intended
   upgrade state and supported path.
5. **Gateway recovery cleanup** — only after production health is stable,
   restore the intended Service selector and remove temporary overrides/policy
   through the documented deployment/config source of truth.

## Guardrails

- Do not delete replicas, snapshots, volumes, PVCs, registry data, or Longhorn
  resources as a diagnostic shortcut.
- Do not force-detach or change replica counts until the data-bearing replica
  and a usable backup are confirmed.
- Before changing an attached volume or node disk, identify its workload and
  record the current replica map and backup status.
## Evidence update — 2026-11-06 USB scheduling decision

The operator policy is to allow replica scheduling on USB disks that are SMART-healthy. Live verification found both relevant disks `Ready=True`: `wynk/usb-longhorn` and `visor/longhorn-ssd`. Recorded SMART evidence is positive for both: the wynk Samsung 990 EVO Plus reports `PASSED`, 0% used, zero media/data-integrity errors, and zero error-log entries; the visor Samsung SSD record reports `PASSED` with zero reallocated, uncorrectable, or CRC errors. The disk-health CronJob completed successfully, while its scope remains limited to node/disk health and not full Longhorn data-path I/O.

The live settings were restored to `allowScheduling=true` on both disks with `evictionRequested=false`. Verification reported `visor longhorn-ssd=true` and `wynk usb-longhorn=true`. Existing replicas were not evicted or deleted. This decision does not establish that the currently degraded volumes are healthy: registry, Wazuh, and Hindsight rebuilds continue to show Longhorn engine/replica RPC failures and require separate investigation.

The remaining provenance gap is host-side correlation of the current rebuild errors with node-local I/O and network paths. Do not treat SMART `PASSED` as a substitute for Longhorn volume robustness or a successful registry read/pull test.

## Evidence updates

Append timestamped findings and verification here as the work order proceeds.
The earlier USB T5 incident is documented in
`docs/incidents/2026-08-26-usb-ssd-flap.md`; it describes a different host and
must not be assumed to explain the current `wynk` USB disk symptoms.

## Evidence update — registry service versus Longhorn rebuilds (2026-11-06)

The registry workload is currently serving from the attached volume: the
`registry` deployment rolled out successfully, its pod is `1/1 Running` on
`wraith`, and the Service has endpoint `192.168.199.190:5000`. The registry is
TLS-only; plain HTTP probes returned `400 Bad Request`, while corrected HTTPS
probes returned `200` for `/v2/` and `/v2/_catalog` with the expected
`Docker-Distribution-Api-Version: registry/2.0` header. Registry logs also show
successful HTTPS blob reads, including a `200` response for a 1.66 GB Trivy
blob. The two deployed gateway pods have their pinned registry image already
present and accessible locally.

This confirms the registry application and current read path, not storage
recovery. The Longhorn volume remains `attached`/`degraded` with one `RW`
replica on `wanderer` and one `WO` replica on `warlock`. Rebuild attempts have
repeatedly failed during ~161 GB snapshot transfers with EOF, connection
refused, and replica state errors; Longhorn has auto-salvaged and remounted the
volume multiple times. The latest read-only capture still shows the engine
running on `wraith` with that `RW`/`WO` map and both replica resources running.

Safe action taken: no replica, volume, snapshot, PVC, filesystem, or replica
count was changed. The registry remains available, but its redundancy is not
recovered. Preserve backup `backup-ae49097b9b8f42b7` from 2026-11-01 while the
next investigation correlates the repeated rebuild failures with node-local
I/O and inter-node transport.

## Evidence update — Longhorn engine RPC fault correlation (2026-11-06)
The leading shared-cause candidate is Longhorn issue
[#13943](https://github.com/longhorn/longhorn/issues/13943): a transient
engine gRPC status RPC error can be stored in the same error field as a real
snapshot purge/restore failure, and the manager can then mark a still-running
replica failed and trigger auto-salvage. The issue reports reproduction on
v1.13.0 and a fix targeted to v1.13.1 (with a v1.12.2 backport). This closely
matches our combination of v1.13.0, repeated replica failure/rebuild churn,
auto-salvage, backend unavailability and iSCSI frontend resets. It remains a
leading candidate rather than a proven root cause: the collected registry
manager logs show failed rebuilds and `Unavailable` purge-status RPCs, but the
exact status error has not yet been correlated as the event that marked its
RW replica failed and initiated salvage. The issue is tracked upstream at
<https://github.com/longhorn/longhorn/issues/13943>.

The logs also show Longhorn's snapshot-concurrent-limiter receiving `Unavailable`
while trying to query purge status through a proxy on visor. That confirms
control/RPC instability but is not alone evidence of corrupted snapshot data.
The registry replica on visor was cleaned up after repeated failed rebuilds;
at 05:19 UTC the manager recorded “failed to rebuild too many times,” stopped
monitoring its engine, and detached the volume before reattaching it elsewhere.
This is a material reason to avoid deleting replicas or attempting manual
salvage while the surviving data lineage is not confirmed.

At the latest check, all four affected volumes were still attached/degraded:
the registry volume on `wyzard`, the two Wazuh volumes on `wraith`, and the
Hindsight legacy data volume on `visor`. The attachment node moving between
checks is consistent with continuing recovery churn; it is not evidence that
any volume has recovered. The new Hindsight PostgreSQL volume was previously
observed healthy and is separate from the degraded legacy Hindsight data PVC.
All eight replica custom resources for these four volumes currently report
`running` across `wanderer`, `wisp`, `wraith`, `wynk`, and `wyrm`; that
superficial process state does not clear the volume-level `degraded` status or
prove that the replicas contain matching data. Do not infer consistency from
the replica process state alone.

No v1.13.1 release was available during this investigation, and the upstream
issue's engine retry fix is merged upstream and the v1.13.x/v1.12.x backport
PRs are merged, but the project says it must also be vendored into the
longhorn-instance-manager image. Master instance-manager verification passed
on 2026-10-22. The published v1.12.2-rc4 release notes do not list this fix,
and a release candidate is not an upgrade target. At this investigation date,
v1.13.1 RC1 is scheduled for Nov 11; wait for a supported patch release and
verify its bundled instance-manager image/version and release notes before
planning an upgrade. Do not downgrade or upgrade to a release candidate as an
incident shortcut.

The merged engine change retries fast `Unavailable` purge/restore status RPCs
up to three times within about five seconds. Upstream reports that longer
outages can still mark a replica failed; the fix reduces false failures from
brief resets, but does not replace stabilizing the storage network. Upstream
also reports that increasing `engine-replica-timeout` does not protect against
this particular fast status-RPC failure. In this cluster it remains 8 seconds
for both V1 and V2. `auto-salvage` is currently enabled; disabling it would
only suppress automatic salvage when all replicas are considered faulty, not
prevent a bad replica-failure decision, and could leave affected volumes
faulted awaiting operator recovery. Do not toggle it as a generic workaround.

Keep V1 fast rebuild disabled as a separate precaution, preserve the Nov 1
registry backup, and focus any temporary mitigation on preventing brief
instance-manager/replica status-network resets. Recheck the four volumes and
registry image-pull path after the node/storage transport is stable; the
canary-backed gateway health check is not a substitute for that verification.

## Evidence update — workaround safety check (2026-11-06 05:59 UTC)

The active Longhorn engine-to-replica sync-agent status ports for the four
degraded volumes accepted TCP connections from their current engine
instance-manager pods in a one-shot check. Calico pods were Ready and recent
Calico logs inspected on affected nodes showed no clear BGP/route/conntrack
failure. These checks do not rule out intermittent resets, but there is no
current evidence identifying a bad route, node pair, or policy to change.

Do not increase `engine-replica-timeout`: upstream specifically found this
does not help fast status-RPC failures, and this cluster is already at the
documented 8-second value. Do not disable `auto-salvage` as a generic
workaround: it would not stop a replica being marked failed and could leave
volumes faulted without automatic recovery. Do not reduce rebuild concurrency
while four volumes are degraded, since that would slow their recovery without
addressing the status-RPC failure. The currently published v1.13.0 instance
manager lacks the merged retry change; replacing it with a custom development
image would require a cluster-wide instance-manager transition while these
volumes are degraded, so that is not a safe incident-time workaround.

Current bounded containment remains: keep disk scheduling aligned with the
documented operator policy (USB disks are allowed when SMART and Longhorn
readiness checks pass), V1 fast rebuild disabled for the separate
snapshot-integrity precaution, registry backup preserved, and avoid
force-detach/replica deletion/manual salvage until each volume's data lineage
is confirmed. Continue collecting status-RPC and Calico/host transport
evidence; deploy the retry fix only in a supported Longhorn release or after
validating a controlled instance-manager rollout procedure with healthy
backups and a maintenance plan.

## Rectification update — registry rebuild (2026-11-06 06:14 UTC)

The live `wynk/usb-longhorn` gate was briefly set to `allowScheduling=false`
while treating the earlier retirement note as current policy. Re-reading the
updated operator decision in `docs/incidents/2026-09-12-wynk-usb-disk.md`
showed that SMART-healthy USB disks remain schedulable, so the gate was
restored to `allowScheduling=true`, with `evictionRequested=false`. No
replica was manually deleted or evicted. The disk status condition remained
`Schedulable=True`; the current spec now matches the documented policy.

The in-flight replacement replica on `wynk` subsequently failed. The engine
events first show the source RW replica marked `Faulted`; the later rebuild
attempt then received `connection refused` from its sync-agent port
(`192.168.160.251:16951`) as the engine stopped/restarted that replica. The
source replica later restarted on a new port. This confirms unstable
instance-manager/replica RPC behavior during recovery, but the refused sync
connection followed the `Faulted` event and is not evidence that it initiated
the failure. The exact initiating error remains unconfirmed.

Longhorn then created replacement replica
`pvc-b40bcefc-8144-4b0b-a92f-06e2392fbcb5-r-4b3fdfa2` on `warlock`'s
`longhorn-ssd` disk (`/mnt/longhorn-ssd`, Samsung SSD 850 EVO 1 TB, SMART
`PASSED`). The registry's existing RW replica `r-e99c432d` remains on
`wanderer`. At 06:45 UTC, the engine map had `r-e99c432d` on `wanderer` and
`r-4b3fdfa2` on `warlock` in `RW` mode, and the volume reported
`attached/healthy`. A third replacement replica `r-a4174950` on `wraith` was
still `WO` and rebuilding at 2%; leave Longhorn to reconcile it rather than
manually deleting it. No further auto-salvage had occurred since 06:09 UTC.

The registry pod remained ready on `wraith`. An HTTPS request to `/v2/`
returned `200`, and at 06:45 UTC the Trivy client read manifests and image
blobs through the registry with successful `200` responses, including blobs
of about 11 MiB and 28 MiB. That confirms registry content reads after the
volume recovered. Continue monitoring the extra `WO` replica, then move to
the Wazuh worker PVC as the next volume in the work order.
## Evidence update — current volume recovery and safe containment (2026-11-06 06:35 UTC)

The current live state narrows the incident: the three previously degraded
attached volumes are now `healthy` (`pvc-349...` Wazuh worker,
`pvc-4b690...` Wazuh master, and `pvc-f6c33...` Hindsight). The registry
volume remains the only degraded attached volume.

The registry engine is `running` on `wraith` with one `RW` replica on
`wanderer` (`r-e99c432d`) and one `WO` replacement replica on `warlock`
(`r-4b3fdfa2`). The replacement is still rebuilding; no replica, snapshot,
volume, PVC, or backup was deleted or detached. The existing `RW` replica and
the Nov 1 backup remain the protected recovery sources.

From a gateway pod, the registry returned `200` for both `/v2/` and
`/v2/_catalog`. This proves current registry API readability and catalog
accessibility, not replica convergence or a successful image pull.

The safe remediation is therefore containment rather than a Longhorn control
change: leave the active engine and `RW` replica untouched, allow the
non-destructive `warlock` replacement rebuild to continue, and re-check the
replica map before testing an actual image pull. Do not reduce rebuild
concurrency, increase `engine-replica-timeout`, disable `auto-salvage`,
force-detach, delete replicas, or alter replica counts while the data-bearing
replica and backup lineage are being preserved.

The earlier `FailedRebuilding`/`FailedSnapshotPurge` events remain evidence of
intermittent Longhorn engine/replica RPC instability. All Longhorn nodes and
instance-manager pods are currently Ready; no single node, disk, Calico pod,
or route has been identified as the cause. The instability remains an
operational risk even though the registry API is presently available.
## Validation update — bounded live check (2026-11-06 06:40 UTC)

The bounded post-containment check found exactly one degraded Longhorn volume:
the registry PVC. The registry engine remains `running` and attached on
`wraith`; its existing `wanderer` replica remains `RW`, while the
`warlock` replacement remains active but not yet `RW`. The three previously
degraded Wazuh/Hindsight volumes remain `healthy`.

From the gateway pod, the registry returned `200` for `/v2/` and
`/v2/_catalog`, and the exact pinned gateway manifest returned `200` with
digest `sha256:7e14b53c2c9b0021b340e407c47f292e3fd0fef01ea7cdcd344ba2a438a76207`.
The production and canary gateway pods both returned `200` for local
`/health` and `/ready` checks. This verifies current registry readability,
manifest access, and gateway readiness; it does not prove a kubelet image
pull from a new node or Longhorn replica convergence.

Remaining blocker: wait for the replacement replica to become `RW` and the
registry volume to report `healthy` before attempting a real image pull. Do
not force-detach, delete or manually salvage the registry replica, change
replica counts, or alter Longhorn retry/concurrency settings during this
wait. Host-side I/O and transport correlation also remains incomplete because
the documented external SSH path to the storage nodes was unavailable.

## Recovery checkpoint — all affected workloads (2026-11-06 06:48 UTC)

Longhorn now reports all four affected volumes `attached/healthy`:

| Workload / PVC | Attached node | Replica state at check |
|---|---|---|
| Registry `pvc-b40bcefc-8144-4b0b-a92f-06e2392fbcb5` | `wraith` | Two `RW` replicas on `wanderer` and `warlock`; one extra `WO` replica on `wraith` rebuilding at 11% |
| Wazuh worker `pvc-349b4b96-4e3d-4119-9dee-7f16092cc5b2` | `wraith` | Two `RW` replicas on `wynk` and `wraith` |
| Wazuh master `pvc-4b69022e-f1a4-4d39-bf64-2a3bc4f841e4` | `wraith` | Two `RW` replicas on `wraith` and `wisp` |
| Hindsight legacy data `pvc-f6c33cc3-2fc7-42f1-b4af-2c4a15ea8a55` | `visor` | Two `RW` replicas on `visor` and `wyrm` |

Completed Longhorn backups remain present for the registry (Nov 1), Wazuh
worker/master (Nov 3), and Hindsight (Nov 1). Wazuh master/worker and both
Hindsight pods are currently `1/1 Running`. The registry endpoint is present;
Trivy successfully fetched registry manifests and blobs with HTTP 200 after
the volume became healthy.

The gateway Service selector is `app=tusker-gateway` and its endpoint is the
production pod on `wyrm`, not the config canary. Read-only port-forward checks
returned HTTP 200 from both `/health` and `/ready`; readiness reported no
degraded pools, and the DB-backed state store was configured and not degraded
at config generation 1160. The production pod has 9 historical restarts (most
recent about 39 minutes before this check), so continue watching it while
Longhorn reconciles the extra registry `WO` replica. No manual replica
deletion, volume detach, or replica count change was performed. The
`wynk/usb-longhorn` scheduling policy remains `allowScheduling=true`,
consistent with the SMART-healthy USB disk policy in
`docs/incidents/2026-09-12-wynk-usb-disk.md`.

The affected volumes and gateway have recovered, but this does not identify
the initiating storage/RPC fault or replace the pending upstream
instance-manager retry fix. Keep the incident open for the registry extra
replica reconciliation, recurrence monitoring, and supported Longhorn patch
rollout planning.

## Gateway recovery validation (2026-11-06 06:50 UTC)

The public `ai.tusker.net.au/health` endpoint returned HTTP 200. Public
`/ready` returned `status=ok`, primary pool `code`, no degraded pools, and
`state_store.degraded=false`. The repository's secure production smoke
(`k8s/smoke_production.sh --timeout 120`) returned
`{"ok":true,"reason":"ok","http_status":200}` for an authenticated
`hermes-privacy` chat request. The registry and the three Wazuh/Hindsight
volumes remained `attached/healthy` at the same check. The extra registry
replica remained `WO` and was rebuilding at 21%, while the required two
replicas remained `RW`; leave it to Longhorn to finish and continue monitoring
for regression. The initiating storage/RPC fault remains unresolved despite
service recovery.
## Validation update — node registry resolution and production route (2026-11-06 06:47 UTC)

The deferred kubelet image-pull check initially failed on `warlock` with
`dial tcp 10.0.9.10:5000: connect: invalid argument`. This was not a registry
pod or Longhorn read failure. The registry hostname resolved inconsistently:
six healthy nodes had the existing `/etc/hosts` pin to the registry ClusterIP
`10.103.195.90`, while `warlock`, `wraith`, `wanderer`, `wisp`, and `warden`
had no pin and resolved the LAN Traefik VIP `10.0.9.10`. The VIP is a valid
external LoadBalancer address, but its port 5000 was not reachable from the
node-host pull path; the ClusterIP was reachable and returned the registry
API successfully.

The established ClusterIP pin (`10.103.195.90 registry.tusker.net.au`) was
added to `/etc/hosts` on those five nodes. A real `imagePullPolicy: Always`
pull of the pinned gateway image on `warlock` then completed successfully in
80 ms and the temporary check pod was removed. This is a node configuration
repair performed outside this repository; it must be made persistent through
the node configuration owner rather than treated as durable from the live
`/etc/hosts` edits alone.

The live `tusker-gateway` Service selector was also corrected from the
temporary `app=tusker-gateway-config-canary` override to `app=tusker-gateway`,
matching `k8s/service.yaml`. The Service now has the production endpoint
`192.168.121.108`; production local `/health` and `/ready` both returned `200`,
and a Traefik pod reached production `/health` through the Service.

The registry volume subsequently reached `attached/healthy` with two `RW`
replicas on `wanderer` and `warlock`; Longhorn was rebuilding an extra `WO`
replica on `wraith` at 11% at the recovery checkpoint. Trivy then fetched
registry manifests and blobs with HTTP 200. No replica, volume, snapshot, PVC,
backup, or replica count was manually changed.

The remaining node repair is durable configuration: the ClusterIP hosts pin
was added live to five nodes and must be moved into the node configuration
owner. Host-side I/O correlation remains incomplete; the current evidence
does not justify destructive Longhorn operations or a release change.
## Final bounded validation — recovery and host correlation (2026-11-06)

The final read-only check reports all four affected volumes `attached/healthy`:
the registry, Wazuh worker, Wazuh master, and Hindsight legacy-data volumes.
The registry engine is `running` with two `RW` replicas and one additional
`WO` replica still rebuilding at 21%; the extra replica is Longhorn-managed
and was not manually altered. The registry pod remains `1/1 Running` on
`wraith`.

The real kubelet pull verification succeeded on `warlock` after the live
ClusterIP hosts pin was added: the exact pinned gateway image completed with
`imagePullPolicy: Always`, then the temporary pod was removed. Production
gateway local `/health` and `/ready` both returned `200`; the gateway Service
selector is `app=tusker-gateway` and its endpoint is the production pod.

The documented read-only SSH path now reached the affected hosts. Kernel
and journal queries for the incident window returned no matching SCSI,
filesystem, USB, Calico, CNI, conntrack, link, or network-timeout lines on
`wraith`, `wanderer`, `warlock`, `wynk`, or `wisp`. This is negative evidence
only: the earlier visor-side SCSI/EXT4 errors and Longhorn RPC failures still
establish the incident, while the initiating node-local path remains
unidentified.

No destructive storage operation was performed. Remaining operational work is
to persist the five `/etc/hosts` pins through the node configuration owner,
observe the extra registry replica to completion/reconciliation, and plan a
supported Longhorn patch release after reviewing its bundled
instance-manager fix.
## Operational-risk closure update (2026-11-06)

The registry hostname pin is now durable rather than live-only. The new
`/Volumes/dev/dev/k8s/` maintenance files idempotently maintain the
`/etc/hosts` mapping; `registry-host-pin.service` runs before `kubelet.service`
and `containerd.service`. The deployment helper installed and enabled it on
all 11 nodes. Fleet verification reported `enabled active` and
`10.103.195.90` on every node, including `wraith` through its node IP.
Forced-placement `imagePullPolicy: Always` checks succeeded on all five
previously affected nodes: `warlock`, `wraith`, `wanderer`, `wisp`, and
`warden`.

The registry volume remains `attached/healthy`; its engine is `running` with
two `RW` replicas and one Longhorn-managed `WO` rebuild. The rebuild advanced
from 53% to 99% during the observation window and remains in progress, with no
error and no loss of volume robustness. This is healthy background
reconciliation, not a reason to force-detach or delete anything.

The shared RPC fault has a durable network fix: Calico node-to-node mesh is
enabled in `manifests/calico/bgpconfiguration.yaml` and live. The measured
pod-to-pod path changed from 105–148 ms with 10% loss to 0.3–2.0 ms with 0%
loss; rebuild throughput improved from 1.45 to 25.5 MB/s. All instance-manager
pods are Ready and no new registry-specific RPC failure appeared in the final
check.

Release review found no stable `v1.13.1` or `v1.12.2` release. The available
`v1.12.2-rc4` is explicitly marked unsupported for upgrades, so no RC,
downgrade, or in-incident control-plane upgrade was attempted. The remaining
action is a supported-release watch and a planned, backed-up rollout only
after the instance-manager retry fix is confirmed in the bundled images.

The safe rollout plan is release-gated: preserve and verify backups, wait for
the registry `WO` rebuild to finish, confirm a stable release bundles the
status-RPC retry fix, canary the Longhorn control-plane release during a
maintenance window, then roll engines one volume at a time and verify every
affected volume plus registry pulls. No live rollout is authorized against the
unsupported RC.
## Wazuh indexer discovery recovery — 2026-11-06

The Wazuh indexer outage during this storage investigation was a separate
StatefulSet discovery/configuration defect exposed by the pod restarts. The
generated `indexer-conf` ConfigMap contained only
`discovery.seed_hosts: wazuh-indexer-0.wazuh-indexer`, so restarted pods could
not reliably discover the other two transport peers. The headless Service now
also publishes not-ready pod addresses (`publishNotReadyAddresses: true`), so
the bootstrap DNS names exist before REST readiness is established.

The durable fix is in
`manifests/wazuh/upstream/indexer_stack/wazuh-indexer/indexer_conf/opensearch.yml`:
all three StatefulSet identities are seed hosts. The generated ConfigMap was
applied and the StatefulSet template was pointed at it; the existing pods were
then replaced through the StatefulSet. Live verification showed all three
indexer pods `1/1 Running`, all three discovery endpoints ready, and an
authenticated `_cluster/health` response with `status: green`,
`number_of_nodes: 3`, `unassigned_shards: 0`, and `active_shards_percent_as_number:
100.0`.

No Wazuh PVC, Longhorn volume, replica, snapshot, or backup was deleted or
altered. The earlier six `NODE_LEFT` unassigned primaries recovered as the
three-node cluster re-formed. The temporary rollout-status timeout occurred
while the ordered pod replacement was still progressing; it was not treated as
proof of storage failure.
