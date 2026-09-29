# Project TODOs

## Deploy provenance: digest pinning and drift check (2026-09-29)

`k8s/deploy.sh` pins the image by digest instead of the mutable tag, annotates
both Deployments with `tusker.net.au/{commit,image-tag,image-digest}`, refuses to
overwrite an existing tag (`FORCE_TAG=1` overrides), verifies the kilo worker's
imageID as well as the gateway's, and prints the `k8s/deployment.yaml` pin for the
operator to commit (`TUSKER_PIN_MANIFEST=1` writes it in place). New helpers:
`k8s/lib-provenance.sh` (registry lookups, with an `ssh visor` fallback because
the registry name resolves only inside the cluster), `k8s/pin-manifest.py` (diff
or write the pin) and `k8s/verify-provenance.sh` (drift check across tracked
manifest, live Deployment, pod imageID, `/health`, registry tag and git HEAD;
exit 1 on any disagreement).

Why: the tracked manifest pinned `sha256:0b496866...` while production ran
`sha256:05bf11d7...`, and nothing in the flow could update it - `deploy.sh`
renders with `--local` and applies the tag, and with `imagePullPolicy: Always` a
re-push of that tag would have silently changed what a restarted pod executes
while `/health` still reported the same commit.

Evidence (production deploy deliberately not re-run): `/health`, git HEAD and the
Deployment annotation all report `1ca0a6e8...`; the tag
`swarm-alpine-1ca0a6e8...` resolves to `sha256:05bf11d7...`, equal to the running
imageID of the gateway and of the kilo worker; `verify-provenance.sh` failed on
the stale pin before the fix and reports OK after it; the digest render was proven
with `kubectl set image --local`, the tag guard against the real registry (fires,
`FORCE_TAG=1` overrides) and `verify_image_digest` - extracted from `deploy.sh` -
against the live cluster (passes on the correct digest, fails closed on a wrong
digest and on an empty selector).

## Metrics scrape auth wired end to end (2026-09-29)

Prometheus had been getting `500 configuration_required` from every
`/metrics` scrape: `TUSKER_METRICS_TOKEN` was never set on the
deployment, so the endpoint failed closed by design while the
ServiceMonitor presented no credential at all. Both halves are now wired
and live-verified.

- Credential: dedicated secret `tusker-gateway-metrics` (namespace
  `hermes`, key `token`, 32-byte url-safe; created out of band like the
  vault). Deliberately **not** `hermes-env-vault` / `tusker-env-vault`:
  a secret referenced by the `monitoring` ServiceMonitor can be mounted
  into the Prometheus pod, which must not expose the ~28 provider keys.
  `k8s/split-secret.sh` is untouched.
- `k8s/deployment.yaml`: explicit `TUSKER_METRICS_TOKEN` via
  `secretKeyRef` (the `envFrom: tusker-env-vault` block stays as is).
- `k8s/servicemonitor-tusker.yaml`: `endpoints[].authorization` with
  `type: Bearer` and `credentials: tusker-gateway-metrics/token`.
  prometheus-operator v0.94.0 **inlines** the credential into the
  generated `prometheus.yaml` (`authorization: {type: Bearer,
  credentials: <token>}`), so no `Prometheus.spec.secrets` mount and no
  extra RBAC are required.
- `k8s/networkpolicy.yaml`: the `monitoring` namespace was allowed in the
  live policy but missing from the repo copy; the repo file now matches
  live so a re-apply cannot break scrapes.
- Live evidence (pod `tusker-gateway-68c85dd598-6dx2f`, revision
  `1ca0a6e8`): `Authorization: Bearer <token>` → 200 with the metrics
  body; missing or wrong token → 401 `invalid_api_key`; token unset on
  the deployment → 500 `configuration_required`. `/dashboard` behaves
  identically; `/health` + `/ready` stay unauthenticated for probes.
- Rotation: update the secret value; the operator re-renders the scrape
  config automatically, but the gateway reads the token from the
  environment, so it needs a rollout to pick up a new value (a mismatch
  window returns 401 until then).

**Scrape target verified healthy, after a storage-side recovery.** The
first attempt at this half found Prometheus itself down for an unrelated
reason: its TSDB volume `pvc-528b560c-8307-4fcc-8f8c-579c2b70069f`
(20Gi, `monitoring`, class `longhorn`) was `detached` with
`robustness=faulted`, longhorn-manager looping on "All replicas are
failed, auto-salvaging volume" with 0 replicas brought up, and the
container exiting 1 on `/prometheus/queries.active: input/output error`
(CrashLoopBackOff; `Available=False, reason=NoPodReady` from
2026-09-29T00:10Z). Root cause: that volume's only replica had lived on
node `wytch` since 2026-09-16, and `wytch` has been unreachable since
2026-09-23T15:44:45Z (`Ready=Unknown`; SSH and ICMP both time out).
Longhorn's auto-salvage only starts *existing* replicas, so a
`numberOfReplicas=1` volume whose replica is on a dead node cannot
recover by itself.

Resolution (operator decision; TSDB history accepted as lost): pod and
PVC deleted, the StatefulSet reprovisioned
`pvc-89f2e2a2-805a-4ad7-b7e0-60fa2a8369d8`, and Prometheus returned
Running/Ready with 0 restarts in ~40s. End-to-end scrape proof from
`/api/v1/targets` on that pod: `up{job="tusker-gateway"} = 1`,
`instance="192.168.21.8:8642"`, empty `lastError`, and real samples in the
TSDB (`tusker_requests_total{pool="code", provider="alibaba",
model="deepseek-v4.1-flash", status="ok"}`).

**Resolved 2026-09-29, once `wytch`/`wyzard` came back with their
Longhorn data intact.** Every volume whose only replica was on `wytch`
auto-salvaged onto a live node. Verified: `td-postgres` 1/1 with data
preserved (Flyway: `td_core` at version 14, up to date),
`prometheus-grafana` 3/3 (its PVC was never deleted, so the dashboard DB
came back as-is), `dev-aia/postgres` and `code-audit/sonarqube` 1/1
(sonarqube reindexed itself), `embed/mcp-embed-data` and
`hindsight/hindsight-data` attached and their workloads Ready.
`td-sync` recovered unattended; `td-crypto` needed one
`rollout restart`: its five-day-old sandbox kept failing `connect` with
`SocketException: Operation not permitted` against a DB that was
demonstrably reachable from the same node, i.e. stale pod network state
from the outage, not a policy (neither `NetworkPolicy` nor a Calico
global policy covers `trust-directory`). It came back on a fresh
sandbox and connected normally.

`pvc-528b560c` (the faulted Prometheus claim's leftover: PV `Released`
with reclaim `Retain`, plus its still-present Longhorn volume) was
deleted, which freed that replica's space on `wytch`. Longhorn now
holds 25 volumes: 17 attached/healthy, 8 detached/unknown behind claims
with no running workload (`hermes/tusker-home`,
`pr-agent/openwrt-{ccache,baselines,dl,images}`, `pr-agent/postgres-data`)
- idle-claim cleanup candidates.

Scrape health is 26/35 up. All 9 down targets are
`kube-proxy` (6), `kube-etcd`, `kube-scheduler` and
`kube-controller-manager`: control-plane components that bind their
metrics to localhost, a pre-existing config gap, unrelated to the node
loss. Still open from that incident: `wynk`'s `usb-longhorn` disk is
gone (`storageMaximum 0`, `longhorn-disk.cfg` missing) and
`disk-health-check` jobs keep failing.

## Strength probe (shipped 2026-09-28, commit c53baef)
`tusker_gateway.tools.probe_strength` measures llm-stats-unknown pool
models with a coding/tool-calling question bank and calibrates them
onto the rank ladder via reference models (conflict-checked). First
live result: big-pickle and syn:small:text ~rank 96 (glm-4.7-class),
above gpt-oss-20b (191). Follow-ups:
- synthetic 429s truncate syn:small:text runs (7 failed questions);
  re-run to refresh its estimate (was 61.5 clean, placed at 96 floor).
- Reference pick is manual (--refs); a consistency-preserving auto
  search over ranked pool models would remove that.
- Bank generation: re-probe every reference after any question change.

Active work items that span multiple sessions / PRs. Items live here
until they ship and then move to a release-notes doc.

## llm-stats sync fix deployed? (DONE 2026-09-28)

Commit `89d593c` deployed 2026-09-28 together with `54b726a`
(LLM Stats enforcement modes). Live `/health` reports commit `54b726a`;
image digest verified; `TUSKER_LLM_STATS_ENFORCEMENT=prefer` active
(per_rank=0.1256 spanning ladder, out-of-cutoff models order behind
better-ranked ones instead of being dropped).

Incident context (2026-09-27): a narrow in-pod verification sync
replaced the whole verdict table and dropped ~112 verdicts, briefly
making excluded models selectable. Restored in place by re-syncing
against the live `/status` candidate inventory (157 models, 16 window
exclusions + 11 site seeds confirmed). The sync now preserves rows for
models outside its pair set, so this class of mistake cannot recur.

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

## 2026-09-25 audit remediation pass

Findings and remediation plan: `docs/audit-2026-09-25-gateway-improvements.md`
(implementation status table at the top of that doc). All P0 and P1 findings
plus the P2 durability/ops findings are implemented locally; the full offline
suite passes (~1397 passed, 8 skipped).

### Implemented

- **P0**: deadline + idempotency middleware attached in `app.py`;
  `/metrics` and `/dashboard` fail closed when `TUSKER_METRICS_TOKEN` is
  unset; approvals bound to the caller fingerprint (`native_question.py`,
  wired from `auth.py`); hardcoded dev/MCP keys removed.
- **P1 API contract**: upstream 429 status preserved end-to-end; response
  `model` carries the advertised alias; `_route_target` maps `kind=swarm`
  to the swarm pool.
- **P1 concurrency**: rate-limit `check()` is a single atomic SQL
  transaction with a conditional decrement; trace span stack uses
  `ContextVar` (per-request isolation); circuit-breaker half-open probe
  slots reserved via atomic `UPDATE ... WHERE half_open_probes < ?`.
- **P2 durability/ops**: `permanent_failures` table + startup hydration in
  `app.py`; `client_ip` honours forwarded headers only from
  `TUSKER_TRUSTED_PROXY_RANGES` peers; PII redaction filters card
  candidates with the Luhn checksum and injection patterns are
  `^`-anchored; docs reconciled (README design decisions,
  `docs/enterprise-controls.md` §6, `docs/capability-catalog.md`).

### Delivered (2026-09-25)

- Committed as `a68522c` on `main` (local; push to `origin/main` still
  pending operator decision).
- Deployed to cluster via `rsync` → `./k8s/deploy.sh` on visor. Image
  `registry.tusker.net.au:5000/tusker-gateway:swarm-alpine-20260925235617`,
  digest `sha256:451dbe2d83059fe459899cf1d929d2d076f2adca98d78aced61d13a98851e96b`.
  Deploy script verified image digest on the Ready pod and `/health` reports
  commit `a68522cffc0fc59743e213d090e6d18397ac6be0`.
- Live smoke passed against `https://ai.tusker.net.au`: `/health` + `/ready`
  200 (Postgres state store healthy, config generation 545), response
  `model` carries the advertised alias (`hermes-code`), prompt-injection
  directive blocked with `guardrail_blocked`, idempotent replay accepted,
  SSE chat completed, `/metrics` without token fails closed (500
  `configuration_required`, by design per `app.py` middleware). Note
  (2026-09-29): that fail-closed 500 was reaching Prometheus on **every**
  scrape because no scrape credential existed; see the metrics section at
  the top of this file.

## 2026-09-26 live audit + remediation

Findings and evidence: `docs/live-audit-2026-09-26.md`. Three P0s fixed,
deployed, and live-verified.

### Shipped

- **P0 quality clobber**: `QualityDB.prime_model()` no longer resets learned
  scores on pool rebuild (`tusker_gateway/quality.py`). Live proof: the two
  `ollama-cloud` rows kept `2.0` through the post-deploy pod restart +
  rebuild; all five previously-clobbered rows now read `2.0`.
- **P0 cosmetic DB disables**: `config_store._load_db()` now merges
  `tusker_config_provider_settings` into `disabled_providers` /
  `passthrough_disabled_providers` (`tusker_gateway/config_store.py:362`).
  The 09-13 `cerebras` / `nvidia` / `github-copilot` disables now actually
  take effect; admin UI toggles are no longer cosmetic.
- **P0 dead `apim` provider**: disabled via deployment env lists AND the
  config DB (both surfaces agree). Privacy pool: configured 301 → 47,
  selectable 13. Code pool: github-copilot's 44 candidates removed.
- Regression tests: 2 in `tests/test_quality.py`, 3 in
  `tests/test_config_runtime.py`. Full offline suite: 1405 passed, 8 skipped.
- Deployed revision `6a7b795` (image digest `sha256:d44d4770d96e...`),
  `/health` verified, tool-call SSE smoke passed live.

### Shipped — follow-up remediation (durable auth failures)

- **Durable auth-failure classification**: upstream 401/403 no longer classified
  as transient `unavailable` by the qualification probes. `endpoints.py` emits
  `X-Tusker-Provider-Failure: provider_auth`; tool/modality/structured probes
  record `auth_failed` (`ToolCapabilityLevel.AUTH_FAILED`), re-probe only after
  `TUSKER_*_QUALIFICATION_AUTH_RETRY_SECS` (default 7 days), mark the route
  permanently failed (in-memory + `permanent_failures` table), and the runners
  skip permanently-failed candidates with a `skipped_permanent_failures` log.
  `pools.py` hard-denies `AUTH_FAILED` in the tool gate. Suite: 1407 passed,
  8 skipped. See `docs/live-audit-2026-09-26.md` §"Follow-up remediation".
- Deployed revision `9cff1db` with image tag
  `swarm-alpine-9cff1dbec458969661a43f44695a35a76c14a877`.
- Live verification: `/health` reports the revision; non-stream and streaming
  chat (including tool calls) and `/v1/responses` returned HTTP 200. The live
  qualification runner completed with valid JSON records. Known 401/403 routes
  were circuit-open, so the upstream `provider_auth` header path was covered
  by contract tests rather than a live upstream auth failure.

### Open offering gap

- `/v1/embeddings` has no healthy configured route: `hermes-code` and
  `voyage::voyage-3` returned 503, while the tested `openai-codex`, `google`,
  and `alibaba` embedding routes returned `unsupported_provider`.

### Open (operator)

- **P1** `openai-codex` credential `553921284e...` 401s on every call —
  re-enroll via `tusker_gateway.tools.enroll_codex_credential` or delete it;
  the rotator only covers it for 2 models via `credential_model_exclusions`.
- **P1** `opencode-go` monthly quota exhausted (35 code + 12 premium + swarm
  `deepseek-v4-flash` degraded) — waits for reset or plan upgrade.
- **P2 candidates** (see audit doc): groq TPM-413 model exclusions,
  `workers-ai` 403 permission check, Google/OpenRouter non-chat catalog
  filtering, dead `ollama-cloud deepseek-v4-flash` variant exclusions.

### Resolved (2026-09-27): LLM Stats coverage beyond the top-50 API window

`/stats/v1/rankings` caps at 50 models and ignores all pagination
parameters. The website homepage embeds the same category rankings to
depth ~375. Resolution: option 3 — explicit website seed import — is
implemented (`tusker_gateway/tools/import_llm_stats_seed.py`, evidence
`site_seed`, 7-day TTL via `TUSKER_LLM_STATS_SEED_MAX_AGE_SECS`).
Mechanics: `docs/gateway-model-routing.md` "Website seed provenance";
investigation: `docs/llm-stats-coverage-2026-09-27.md`.
Follow-up option remains open: ask upstream for API pagination.

## OpenCode warm-server path (shipped 2026-09-28)

`opencode-cli` requests previously paid a private `opencode serve` start per
request (about 6s). The adapter now keeps one server alive and publishes each
request through the Kilo-style broker pointer, because a server fixes its MCP
configuration at start.

- Code: `tusker_gateway/provider_adapters/opencode_cli.py` (server lifecycle,
  broker pointer, serialized requests, per-request session delete).
  `TUSKER_OPENCODE_WARM_ENABLED=1` by default; `TUSKER_OPENCODE_WARM_IDLE_SECS`
  (default 300) releases the server; `TUSKER_OPENCODE_WARM_BROKER_DIR` overrides
  the pointer directory.
- Manifest: `k8s/deployment.yaml` enables the path with a 1800s idle window and
  raises `resources.limits.memory` 1Gi to 2Gi. Evidence: the warm server
  measured ~160MiB resident, the gateway process alone ~600MiB RSS, and a
  transient `opencode run` client adds ~200MiB; the 1Gi limit left no margin.
- Tests: 9 new cases in `tests/test_opencode_cli_adapter.py` (26 in that file).
  Full offline suite: 1470 passed, 8 skipped.
- Live verification (in-pod, modified package copied to `/tmp`, production Zen
  endpoint): standalone 7.9s/10.2s versus warm 13.9s first (includes the server
  start) and 5.7s reused; a tool call returned through the broker pointer with
  `{"interrupted":true}` plus session delete; streaming frames and clean finish;
  sessions 17 to 17 across four requests, so the deletes bound the server's
  session store.
- Docs: `docs/provider-adapters.md` section "Warm server mode".

### Delivered (2026-09-28)

- Committed as `16220b2` on `main`; push to `origin/main` remains an operator
  decision, as with the previous passes.
- Deployed from the exported tree
  `/srv/opencode/tusker-ai-gateway-build-16220b2a223fb43454ddb534116fe71c7cd2cd19`.
  Image `registry.tusker.net.au:5000/tusker-gateway:swarm-alpine-16220b2a223fb43454ddb534116fe71c7cd2cd19`,
  digest `sha256:b9cc71e7a9c95bac292846c41a1d1bbac37ee5f3f97b90b13a8df141fc37d6b5`.
  `deploy.sh` verified the Ready pod's image digest, the `/health` commit, and
  `/health` + `/ready` 200 plus the chat SSE smoke; 87s build-to-smoke.
- Post-deploy verification ran in the new pod against the installed package:
  `pid1: tini`, warm 4.9s first and 3.1s reused against 5.5s standalone, a tool
  call returning `report_value {"value":"deployed-ok"}` with
  `{"interrupted":true}` plus session delete, streaming frames with a clean
  finish, sessions 1 to 1, and no zombies after the server was stopped.
  The `tini` entrypoint closed the finding below: the same run that previously
  left a zombie per server release reports `zombies after warm traffic: []`.
- Per-adapter warm decision recorded in `docs/provider-adapters.md` ("Which
  adapters can keep a server warm"): `kilo_cli` and `opencode_cli` keep a
  server; `claude_code` does not, because the CLI has no server mode and its
  init measured 0.14-0.15s as a native binary, so a warm server has nothing to
  save (OpenCode's server start is about 6s by comparison).

### New finding: unreaped children in the gateway container

The container runs no init, so the gateway is PID 1 and must reap orphaned
children itself. `/proc` shows 7 zombies (`comm=opencode`, `ppid=1`) left by
CLI-spawned servers and workers that outlived their parent. Every CLI adapter
(`claude_code`, `kilo_cli`, `opencode_cli`) can produce them.

Fix applied in this pass: the image now runs `tini` as PID 1 (installed after
the dependency layers so the torch/chromadb/model cache is not invalidated).
An init is the right place for this rather than a `waitpid(-1)` loop in the
gateway, because asyncio's child watcher owns its own children and a blanket
reap would race it and break request-path waits. Verified live: PID 1 reports
`tini` and no zombies accumulate.

### Destructive actions (authorized and executed 2026-09-28)

- `kubectl apply` on `k8s/deployment.yaml` (warm env plus the memory limit).
- `./k8s/deploy.sh $REV` rollout; `strategy: Recreate`, so the gateway was
  briefly unavailable during the 87s build-to-smoke run.
- Not done, still requiring confirmation: `git push` to `origin/main`, and
  pruning the visor build directory for this revision.

## Embeddings and rerank end-to-end (audited 2026-09-28)

Both media surfaces work end to end in production. Verified live after
restoring the gateway; every case below returned HTTP 200:

- `/v1/embeddings`: no model, `hermes-embed`, `local-llm/nomic-embed-text`
  pin and a 3-input batch all resolve to the Jetson
  (`http://10.0.0.212:11434`, `nomic-embed-text`, 768 dims), and the returned
  vector is byte-identical to a direct call to that endpoint.
  `voyage/voyage-3` pin returns 1024 dims.
- `/v1/rerank`: `hermes-reranker` and `cohere/rerank-v3.5` both resolve to
  Cohere `rerank-v3.5` with correct ordering (0.8332 for the matching
  document) and `top_n` honored.

### Defect A (reproduced live): one bad model name takes the whole surface down

A request whose model name no backend accepts poisons every configured
backend:

- It is not rejected. `EmbedHandler._resolve_model` (`providers/embed.py:212`)
  and `RerankHandler._resolve_model` (`providers/rerank.py:198`) only reject
  names pinned to a *known* provider, so an unknown bare name is forwarded to
  every backend as an upstream model override (`backends_for_model`:
  `embed.py:269`, `rerank.py:246`).
- Every backend then fails, and `_mark_failure` (`embed.py:548`,
  `rerank.py:617`) records a breaker failure plus a cooldown. The window comes
  from `_cooldown_seconds_for_provider_error` (`cooldown.py:439`), which maps
  any non-5xx, non-quota 4xx to `PERMANENT_ERROR_COOLDOWN_SECS` (3600s
  default). Three consecutive failures additionally arm a provider-wide window
  (`embed.py:582-585`, `rerank.py:648-651`).
- Observed: a probe with `model=hermes-code` created cooldowns for
  `(local-llm,hermes-code)`, `(synthetic,hermes-code)`,
  `(openrouter,hermes-code)`; a probe with `model=x` created
  `(cohere,x)`, `(voyage,x)`, `(jina,x)` — exactly the two provider sets.
  Every later request, from any client and for any model, then returned
  `503 no_healthy_models` in 0.0s because `is_cooldown` skipped every backend.
- The state is durable: it lives in the state DB and is hydrated at startup
  (`app.py:316-320`), so it survives restarts for the full hour.

Fix direction: reject unknown models (and names pinned to unknown providers)
with 400 `unsupported_model` on both routes instead of forwarding them, and
exclude client-caused 4xx from failure accounting and long cooldowns — only
genuine provider failures (429, 5xx, auth/not-found for a valid model) should
cool a backend.

### Defect B (latent): the DB config store cannot express `embed_path`

- `tusker_config_providers` persists `rerank_path` but has no `embed_path`
  column, and `_apply` (`config_store.py:314`) builds `ProviderConfig` without
  it.
- `ConfigRuntime._rebuild_media_handlers` (`config_runtime.py:465`) rebuilds
  image/TTS/video handlers only; `embed_handler`/`rerank_handler` are created
  once from the startup config (`app.py:283-284`). Net effect: embeddings and
  rerank read the static registry (which has `embed_path`) and so work, but
  silently ignore DB provider/key edits until a restart, and would lose
  embedding support entirely if repointed at the DB registry.

### Gap C: `/v1/embeddings` requires no scope

`identity.py:252` resolves the required scope from `_ROUTE_SCOPES`. `/v1/rerank`
is listed as `inference:rerank` but `/v1/embeddings` is not, so
`required_scope` is `None` and the scope check is skipped entirely for
embeddings. Pool gating still applies (`identity.py:268-270`: `/v1/rerank` →
`rerank`, everything else → `media`), as do caller model/provider allowlists
(`identity.py:256-266`). Effect: a caller deliberately denied rerank scope
retains embeddings access, so least-privilege granularity is inconsistent
between the two media surfaces.

### Gap D: no media capability qualification

Chat has capability qualification and permanent-failure gating (`pools.py` and
the qualification modules); embeddings and rerank have neither, so cooldown and
breaker state is the only health signal.

### Incident note (self-inflicted, resolved)

The cooldown state in Defect A was created by this audit's own probes. It was
cleaned by deleting exactly those 6 model rows and the 5 provider-wide rows
(no breaker, permanent-failure, key or config row touched), then
`kubectl rollout restart deployment/tusker-gateway` reloaded a clean tracker;
`/status` cooldowns for those providers then read empty and every case above
returned 200.

### Fix applied (2026-09-28)

- **Defect A.** `_resolve_model` in `providers/embed.py` and
  `providers/rerank.py` now rejects a model that matches no configured backend
  with 400 `unsupported_model` (naming the accepted models), and a bare name
  that matches a configured backend model pins that provider instead of being
  broadcast to every backend. New shared helper `cooldown.is_request_level_error`
  makes `_mark_failure` in both handlers record no breaker failure and no
  cooldown for a client-caused 4xx (quota-shaped bodies keep the long window),
  so one bad request can no longer quarantine the route. The reject is logged
  with its upstream status.
- **Defect B.** `tusker_config_providers` now carries `embed_path` (table DDL
  plus an `_ensure_column` upgrade for existing databases), the migration tool
  and the admin provider API read and write it, and
  `ConfigRuntime._rebuild_media_handlers` rebuilds the embed and rerank
  handlers when provider names, base URLs, embed/rerank endpoints or keys
  change instead of leaving the startup instances in place.

Verification: 12 new tests across `tests/test_embed_provider.py`,
`tests/test_rerank.py` and `tests/test_config_store.py` (unknown model rejected
without upstream contact, configured bare name pins, client-4xx records nothing,
auth failure still cools, quota body still cools, `embed_path` round-trip,
legacy-database column upgrade). Full suite:
`pytest tests/ -p no:cacheprovider --ignore=tests/test_passthrough_providers.py`
→ 1482 passed, 8 skipped. Throwaway proof of the rebuild path: an unchanged
provider fingerprint preserves the handler instances, an endpoint edit rebuilds
both, and the rebuilt embed handler reads the new provider config.

Not in this pass: Gap C (`/v1/embeddings` requires no scope) and Gap D (no
capability qualification for media routes).
