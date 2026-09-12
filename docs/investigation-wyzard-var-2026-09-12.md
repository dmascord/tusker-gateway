# Wyzard `/var` crowding — investigation & options

Status: **investigation complete, no destructive LVM ops performed** as of
2026-09-12. The 2026-09-12 gateway outage was unblocked by ad-hoc cleanups
(rotation of `/var/log/journal`, deletion of `/var/log/atop/atop_202609{07..11}`,
truncation of `/var/log/calico/cni/cni.log`, and `fstrim -v /var`). Those
cleanups bought ~500 MB of breathing room and the gateway rollout completed
on the same node. **They do not solve the structural constraint** —
wyzard's `/var` LV is fundamentally too small for the workload it now
hosts, and the next image pull (640 MB gateway image) or qdrant WAL
expansion will re-fill it.

## Symptoms observed

- `df -h /var` on wyzard reports `/dev/mapper/wyzard--vg-var` at 99% used,
  ~42 MB free.
- New pods scheduled on wyzard fail to start with `no space left on
  device` during CNI sandbox creation. Symptom resolves when image
  pulls and Longhorn engine installs temporarily consume the buffer
  freed by `fstrim`.
- The misread "193 GB under `/var/lib`" that initially caught us out is
  misleading: those numbers come from `du` walking past mountpoints into
  the docker (190 GB), containerd (32 GB), and qdrant CSI bind-mount
  filesystems, which sit on **other** LVs.

## What actually lives on the 3.2 GB `/var` LV

Verified by `du -sh` after the cleanup pass (excluding `/var/lib/docker`
and `/var/lib/containerd` which live on different LVs):

| Path | Size | Notes |
|---|---:|---|
| `/var/lib/longhorn/engine-binaries` | 135 MB | v1.11.2, v1.12.0, v1.12.1 — `~45 MB × 3`. **Longhorn auto-redownloads these whenever the `LonghornVersion` setting references them; deleting is futile** unless you also reset the setting. |
| `/var/lib/kubelet/pods/<qdrant-uid>` | ~371 MB | Active CSI bind-mount for the qdrant vector database. **Currently-running pod** (`qdrant-57767f855b-5knmd`, 9d old). Each segment file is 32 MB; total grows with ingested embeddings. |
| `/var/lib/dpkg/info` | ~34 MB | Mostly metadata for the kernel 6.12.101 headers package. Unavoidable until the kernel package is purged. |
| `/var/log` (current state) | ~73 MB | Mostly pod logs and journal. |
| `/var/log/pods` | 48 MB | K8s container log archive. |
| `/var/log/atop` | 8 MB | Only current-day file remaining after cleanup. |
| remainder (cni, systemd, journald, apt cache) | ~50 MB | |
| **Total on `/var` LV** | **~720 MB** out of 3.2 GB after cleanup pass, plus filesystem overhead and CNI/bpf cache. |

The whole LV is genuinely undersized for the workload set.

## Spurious "193 GB" figured out

`du -sh /var/lib` reports 193 GB because it walks through:

1. `/var/lib/docker` → 176 GB reported, but that is the **separate** 190 GB
   `wyzard--vg-docker` LV. (`mount` shows `wyzard--vg-docker on
   /var/lib/docker type ext4`.)
2. `/var/lib/containerd` → 17 GB reported, but mounted on the **separate**
   32 GB `wyzard--vg-home` LV. (`mount` shows `wyzard--vg-home on
   /var/lib/containerd type ext4`.)

The same applies to every bind-mount nested under
`/var/lib/kubelet/pods/<uid>/volumes/...`. The actual qdrant bind mount
size (verified with `du -sh` directly) is **371 MB**, not the 4.3 GB
initially seen from a Python `os.path.getsize` summation (which
double-counts `mount` and `globalmount` paths of the same CSI bind).

## Surprising findings

- `/dev/sdc` (4 GB), `/dev/sde` (10 GB), `/dev/sdf` (2 GB) appeared to
  be spare disks but are **not**. Each is a Longhorn-managed raw block
  device backing a different PVC — `sdc` is the qdrant disk, `sde` and
  `sdf` are other tenants. None is reclaimable.
- `wyzard-vg` has 960 MB free in the VG, just enough for a small
  `lvextend`. Not enough to make `/var` meaningfully larger; not
  enough for a separate, dedicated `/var` extension.
- The Longhorn engine-binary stack is ~135 MB of **mandatory** overhead
  on every Longhorn data node. Future Longhorn engine version bumps add
  another 45 MB. This is by design; it can't easily be trimmed below
  ~135 MB without changing Longhorn's `EngineImage` setting.

## Node-by-node comparison

```
visor   /var:  315G  149G  154G  50%
wyrm    /var:  7.6G  3.6G  3.6G  50%
wynk    /var:  1.2G  696M  367M  66%   ← cannot accept new workloads
wytch   /var:  760G   54G  676G   8%
wyvern  /var:  9.1G  1.2G  7.5G  14%
wyzard  /var:  3.2G  3.0G   42M  99%   ← the trouble node
```

The natural migration target for the heaviest workloads is **visor**,
which has 154 GB free on `/var` and ample disk otherwise; **wytch** is
also fine for any single workload. `wyvern` and `wyrm` are comfortable
for medium workloads.

## Workloads currently scheduled on wyzard

```
embed/                   qdrant-57767f855b-5knmd        ← 371 MB on /var via CSI bind
hermes/                  tusker-gateway-dd5755dd5-4krz8  ← 1 GB RWX PVC (no /var impact)
kube-system/             calico-node-9gbp9, kube-proxy, coredns, nfd-worker
longhorn-system/         3 × engine-image pods, manager, instance-manager, csi-plugin
mqtt/                    mosquitto-774c9d6654-7d7hx    ← 2 GB CSI disk on /dev/sdf
vault/                   vault-0                       ← small
```

The structural issue is **qdrant**. Its CSI bind-mount eats 371 MB of
wyzard's 3.2 GB `/var` LV. Once that fills its PVC quota (4 GB), Longhorn
will refuse to allocate more — but until then, every new embedding
ingested writes more WAL into wyzard's `/var`.

`vault`, `mosquitto`, and the gateway pod itself are well-behaved and
either use RWX PVCs (gateway) or other LVs (vault/mosquitto via
`/dev/sdf`).

## Realistic fixes (none performed yet)

### Option A — relocate qdrant off wyzard (low risk, modest value)

- Add `nodeName: wytch` (or `visor`) to the qdrant pod spec
  temporarily, or rely on `nodeSelector` /
  `nodeAntiAffinity{op: NotIn values: ["wyzard"]}` permanently.
- Capacity freed: ~371 MB. Still leaves ~400 MB free. Wins another day.
- Cost: a brief qdrant restart; Longhorn will replicate the 4 GB
  volume to the new node automatically. No data loss.
- **Recommended first move.** Small blast radius.

### Option B — resize wyzard `/var` LV (medium risk, structural fix)

`/home` is currently 32 GiB, used 24 GiB. Shrinking `/home` from 32 GiB
to ~22 GiB would release ~10 GiB in the VG, sufficient to grow `/var` to
~13 GiB. Online shrink of a mounted ext4 filesystem requires
`lvreduce` → `fsck` → `resize2fs`. Touchy, but doable offline during a
short maintenance window.

- Capacity freed: structural, the issue doesn't recur.
- Risk: if `lvreduce` hits an active in-use ext4 block, the filesystem
  can become inconsistent. Backup first.
- **Recommended if A isn't enough.** Plan and execute during a low-traffic window.

### Option C — add physical storage (medium risk, structural fix)

If a spare disk can be added to the wyzard host, a new PV can join the
VG and `/var` can be grown cleanly.

- Capacity freed: structural.
- Risk: physical hardware intervention; requires downtime to seat the disk.
- **Recommended if both A and B are insufficient.**

### Option D — leave as-is and rely on rotation

Keep using the rotation cleanup we just did. Risk is that the next
image pull, qdrant WAL expansion, or Longhorn engine version bump
trip the ENOSPC threshold again.

## Recommended plan (proposed, not executed)

1. **Option A** — drain qdrant off wyzard immediately. README update:
   qdrant pod via `kubectl scale deploy mcp-embed --replicas=0` then
   `nodeAntiAffinity` rule before scaling back to 1. (Destructive-class
   but reversible.)
2. **Option B** in a follow-up maintenance window if A is insufficient.
3. **Option C** as a longer-term box-level fix.

## What I did NOT do

- No LVM ops on wyzard. The user asked for an investigation, not
  destructive action.
- No `kubectl delete` of any pod on wyzard. The 192.168.170.144 qdrant
  pod and 192.168.170.161 gateway pod were left running.
- No changes to Longhorn engine-binary retention settings.
- No PVC resize / Longhorn volume migration.
