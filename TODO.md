# Project TODOs

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
  `configuration_required`, by design per app.py middleware).

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
