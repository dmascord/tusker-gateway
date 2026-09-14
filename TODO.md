# Project TODOs

Active work items that span multiple sessions / PRs. Items live here
until they ship and then move to a release-notes doc.

## DB-backed config store (shipped)

Migrate gateway static config (provider registry, pool definitions, API
keys, provider credentials, OAuth token rotation) from env-var / Python
sources into a PostgreSQL-backed `ConfigStore`.

**Status:** wiring complete (app, admin REST endpoints, runtime reload,
OAuth CAS, canary infra, tests). Only the `tusker_gateway/config_store.py`
body remains stubbed.

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

### Test commands

```
# Single-file (currently passes):
python3 -m pytest tests/test_admin_api.py -q                  # 15 passed
python3 -m pytest tests/test_config_runtime.py -q             # 13 passed (in isolation)

# Full suite (5 fail in test_config_runtime.py under ordering pollution):
python3 -m pytest tests/ -p no:cacheprovider --ignore=tests/test_passthrough_providers.py -q
# → "5 failed, 887 passed, 3 skipped, 1868 warnings in 22.91s"
```

### Test-isolation failure (currently reproduced)

```
FAIL tests/test_config_runtime.py::test_runtime_config_returns_store_snapshot_and_updates_generation
FAIL tests/test_config_runtime.py::test_runtime_config_unavailable_retains_last_good
FAIL tests/test_config_runtime.py::test_identity_config_same_pattern
FAIL tests/test_config_runtime.py::test_reload_now_delegates_to_store
FAIL tests/test_config_runtime.py::test_poll_loop_applies_only_on_generation_change
```

**Root cause (confirmed via test isolation):** `tests/test_config_runtime.py`
replaces `sys.modules["tusker_gateway.config_store"]` at module load,
but `config_runtime.py` line 22 caches the `ConfigStore` class when
imported — so a different test file importing the real store BEFORE
this test pollutes the binding. Five `_store()` calls fail
`isinstance(store, ConfigStore)` and silently return `None`, which
breaks `runtime_config()`, `identity_config()`, `reload_now()`, and
the poll loop.

**Fix:** implement the real `ConfigStore` (item 1 above), then rewrite
the test to use a real SQLite-tempfile instance instead of the fake.
The fake-injection hack should be deleted.

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

## 2026-09-14 audit findings

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

### 5. Unpushed local commits (6, ahead of origin/main)
- `0c2facd` observability: use leftmost XFF for Cloudflare, not rightmost — **superseded by Cloudflare proxy mode; recommend revert**.
- `44d2034` observability: prefer CF-Connecting-IP over X-Forwarded-For for client IP — deployed.
- `27b2dc4` observability: log real client IP from X-Forwarded-For — deployed.
- `ab51671` usb-flap-monitor: detect UAS aborts and USB resets, not just disconnects — script exists but timer not installed.
- `d694084` fix(cooldown): strip URLs from 429 body before hint matching; honour x-ratelimit-* headers.
- `7a03ad1` fix(stream): detect mid-stream 429 envelopes as RateLimitError.

### 6. visor tree divergence
- Visor's `/srv/opencode/tusker-ai-gateway` HEAD is at `ca119a3`, which is 65 commits behind `origin/main` (which is at `c79edcf`).
- Visor has many uncommitted modifications and untracked files (uncommitted work in progress).
- Local main has 6 commits ahead of origin/main (the observability/USB/429 fixes listed in #5).
- The cluster runs an image built from a commit (`44d2034`) that's neither in visor's tree nor in origin/main — deployed via direct push to registry, not via visor's `deploy.sh`.

### Destructive actions (require confirmation)
- Removing orphaned replica directories from `/mnt/longhorn-ssd/replicas/` on visor.
- Installing or removing the usb-flap-monitor systemd timer.
- `git push` of unpushed commits to origin.
