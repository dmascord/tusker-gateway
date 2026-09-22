# Project TODOs

Active work items that span multiple sessions / PRs. Items live here
until they ship and then move to a release-notes doc.

## DB-backed config store (shipped)

Migrate gateway static config (provider registry, pool definitions, API
keys, provider credentials, OAuth token rotation) from env-var / Python
sources into a PostgreSQL-backed `ConfigStore`.

**Status:** shipped. `ConfigStore` body implemented (SQLite + PG), DB is
production source of truth (`TUSKER_CONFIG_DATABASE_ENABLED=1`), live admin
write round-trips verified, config hot-reload working (generation 196).

**Full plan:** `docs/migrations/2026-09-09-config-db/IMPLEMENTATION_PLAN.md`
**Schema spec:** `docs/migrations/2026-09-09-config-db/SCHEMA.md`
**Why:** `docs/migrations/2026-09-09-config-db/README.md`

### Open items (in dependency order)

- [x] Implement `ConfigStore` body (SQLite + PG paths, idempotent schema,
      encryption, snapshot/reload/CRUD). See `IMPLEMENTATION_PLAN.md` §1.
- [x] Fix `ConfigUnavailableError` to accept `code=` kwarg
      (currently raises `TypeError` at module import in the stub).
- [x] Implement `persist_credentials` as a CAS callback
      `(provider, expected, replacement) -> bool` (currently returns
      `False`, which would silently break OAuth token refresh).
- [x] Add `tusker_gateway/tools/migrate_config_to_db.py` — idempotent
      env-to-DB migration. See `IMPLEMENTATION_PLAN.md` §2.
- [x] Drop the fake-module injection from `tests/test_config_runtime.py`
      once the real store exists. See `IMPLEMENTATION_PLAN.md` §3.
- [x] Canary smoke-test against `tusker-gateway-c` pod via
      `k8s/config-canary.py smoke --execute`. See `IMPLEMENTATION_PLAN.md`
      §4.
- [x] Flip `TUSKER_CONFIG_DATABASE_ENABLED=1` in `k8s/deployment.yaml`,
      run migration, `rsync`, `./k8s/deploy.sh`.
- [x] Live admin write round-trip: `POST /admin/keys` for Don Gould,
      verify request through `https://ai.tusker.net.au/v1/chat/completions`.
- [x] Update `docs/postgresql-state.md` to list OAuth credentials and
      provider secrets as state-DB managed.
- [x] Update `AGENTS.md` doc index.

### Test results

```
# Full suite (all pass; test isolation failures resolved by real ConfigStore):
python3 -m pytest tests/ -p no:cacheprovider --ignore=tests/test_passthrough_providers.py -q
# → "1149 passed, 3 skipped" (2026-09-22)
```

### Destructive actions (require confirmation)

- `kubectl apply -f k8s/config.yaml` (secrets)
- `kubectl rollout restart deployment/tusker-gateway` (DB schema migration
  requires restart to pick up new tables)
- `kubectl delete deployment tusker-gateway-config-canary` after smoke
- `rm data/config.db` (local dev only — never run on a prod pod)
- Any operation that writes to `pgcrypto`-encrypted columns under a
  different encryption key than the existing rows were encrypted with —
  this permanently bricks the row.

## Longhorn disk `usb-longhorn` on node `wynk` soak test

The USB disk was re-registered on 2026-09-12 as an unschedulable Longhorn
disk. The host auto-recovery agent is active and the disk-health CronJob is
running every five minutes. No filesystem repair or formatting was performed;
no replica is scheduled on this disk.

### Soak items

- [x] Capture current Longhorn state for usb-longhorn on wynk (state,
      node instance-manager logs, PVCs currently bound to it).
- [x] Re-register the disk with `allowScheduling: false` and
      `evictionRequested: false`.
- [x] Fix and deploy the wynk auto-recovery agent; verify it reaches
      `MONITORING`.
- [x] Fix and deploy the disk-health checker so Kubernetes API failures fail
      the job instead of printing a false success.
- [ ] Complete a sustained soak without USB disconnects or ext4/JBD2 I/O
      errors before enabling Longhorn scheduling.
- [x] Document the current soak status under
      `docs/incidents/2026-09-12-wynk-usb-disk.md`.

### Current observation

- `/dev/sdb1` is mounted read-write at `/mnt/kubelet`; ext4 reported `clean`.
- `/mnt/kubelet/longhorn/longhorn-disk.cfg` exists with `state: "ready"`.
- Longhorn reports `Ready=True`; `storageScheduled=0`.
- `allowScheduling=false` remains the explicit safety gate.
- The local recovery agent is `active`, state `MONITORING`.
- The corrected health job reports real failures; its latest run caught a
  transient DNS/API failure instead of falsely passing.

### Destructive actions (require confirmation)

- Any filesystem-level change to `/mnt/kubelet/longhorn` on `wynk`.
- Enabling scheduling on this USB disk before the soak period completes.
- Removing the disk entry again or deleting the Longhorn node registration.

## 2026-09-14 audit findings (revisited 2026-09-22)

Audit done before further action. Items in priority order:

### 1. USB T5 drive state (visor)
- T5 still physically attached (`/dev/sdc`, 931 GB) at `/mnt/longhorn-ssd`.
- Longhorn disk entry exists with `allowScheduling: false, evictionRequested: true`.
- **No live replicas on the disk** (postgres volume has 3 replicas on wyrm, wytch, wyvern).
- **Orphaned replica dirs** on disk from old PVCs: `pvc-2afe302d-*`, `pvc-84387ebb-*`. These PVCs/PVs no longer exist in K8s/LH; data is just leftover on the filesystem (~4KB metadata each, but actual data size unknown without read permission).
- Memory said the drive was the critical path — that's stale. The drive is unused for critical state.

### 2. usb-flap-monitor timer not installed on visor
- Repo has `tusker_gateway/tools/usb-flap-monitor.{sh,service,timer}`.
- `systemctl list-unit-files | grep usb-flap` returns nothing.
- `/usr/local/bin/usb-flap-monitor.sh` does not exist.
- Earlier today the script was deployed to `/usr/local/bin/` (md5 verified), but the systemd timer was not re-installed. The script was later removed during a visor cleanup.
- AGENTS.md updated to note the deployment gap. Decision pending: re-install, or remove from docs since the drive is unused for critical state.

### 3. Registry GC orchestrator fixed (this session)
- Bug: hardcoded `REPOS = ["hermes-agent"]` excluded all other repos (esp. tusker-gateway with 259 tags → 138 GB of blobs).
- Bug: full FQDN `registry.registry.svc.cluster.local` fails DNS in-cluster; switched to `registry.registry.svc`.
- Bug: no `URLError` handling — silent crashes on DNS failure.
- Fix verified: dynamic catalog fetch, all repos cleaned, GC ran, disk at 3% used (4.3 GB / 147.5 GB).
- CronJob `registry-gc-orchestrator` in `registry` namespace runs weekly at `0 4 * * 0`. Next run: 2026-09-20 04:00 UTC.

### 4. Cloudflare proxy mode enabled (2026-09-14)
- DNS for `ai.tusker.net.au` now resolves to Cloudflare IPs (172.67.216.212, 104.21.24.21).
- Direct route to public IP `103.68.121.242:443` from external hosts times out (expected — router firewall blocks non-Cloudflare IPs on port 443).
- Logs now show real client IPs via `CF-Connecting-IP` (e.g. `182.54.232.211` from wildduck, `2405:800:2:1::6e` IPv6 from laptop).
- `0c2facd` (leftmost XFF) is unnecessary now — `CF-Connecting-IP` provides the real IP and takes priority in the code.

### 5. Unpushed local commits (was 6) — RESOLVED (2026-09-22)
- All 6 observability/USB/429 commits were already in main (pushed previously).
- Additional 8 OMP approval commits (`29260aa`..`ae6dafd`) pushed 2026-09-22.
- origin/main now at `ae6dafd`; no divergence.

### 6. visor tree divergence — RESOLVED (2026-09-22)
- Visor's `/srv/opencode/tusker-ai-gateway` reset to `ae6dafd` (origin/main), clean tree.
- Old build directories pruned to 5 most recent (was 80+).

### Destructive actions (require confirmation)

- Removing orphaned replica directories from `/mnt/longhorn-ssd/replicas/` on visor.
- Installing or removing the usb-flap-monitor systemd timer.
