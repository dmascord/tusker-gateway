# Resilience and performance assessment (2026-11-25)

Evidence-backed review of what would make the deployed gateway more resilient
and faster, taken after the context-advisory release (`c38a06c`, image digest
`sha256:2ed14a62…`). Every number below was measured on the live cluster or in
a local benchmark; conclusions that could not be verified are marked as such.

This document records **findings only**. It does not change runtime behaviour.
Actionable work is tracked in `TODO.md`.

## Method

- Live metrics read in-pod from `GET /metrics` and `GET /status` with the
  metrics token held only in the pod's environment (never printed).
- Live database inspection via `psql` inside `tusker-gateway-postgres`.
- Live pod state via `kubectl get pods` / `kubectl top pod` on `visor`.
- Local microbenchmarks with the venv Python, against the checked-out source.

## Already addressed — do not spend effort here

These were expected to be gaps and were found to be fixed or non-issues. They
are recorded so a future review does not re-litigate them.

| Suspected gap | Finding | Evidence |
|---|---|---|
| `QualityDB.rank()` N+1 queries | **Fixed.** Ranking uses a bulk CTE over a chunked `VALUES` list and a short TTL cache. Local benchmark: **0.21 ms** for 360 candidates on an empty DB. | `tusker_gateway/quality.py:58-76` (`_read_scores`), `:373-425` (`rank`). Fix landed in PR #72 (`bb6ee39`, `e30a126`). |
| Prompt-token estimation overhead | **Not a bottleneck.** `estimate_prompt_tokens` takes **0.61 ms** for an 800-message / 760,428-token prompt. | Local benchmark `/tmp/est_bench.py`; `_CHARS_PER_TOKEN = 3.2` at `context_limits.py:41`. |
| Gateway request latency dominated by gateway code | **No.** Live histogram for `pool=code,provider=minimax,model=MiniMax-M3.1-Flash` (318 requests): p50 ≈ 5 s, p90 ≈ 10 s, p99 < 60 s. Model time dominates. | `tusker_request_duration_seconds_bucket` from live `/metrics`. |
| Postgres connection saturation | **No.** 13 connections of `max_connections=100`; pool capped at 12 (`TUSKER_STATE_DATABASE_POOL_MAX=12`). | Live `pg_stat_activity` and `show max_connections`. |
| Postgres disk pressure | **No immediate risk.** 930 MB used of 9.7 GB available (9%); database size 791 MB. | Live `df` in the postgres pod. |
| Gateway log errors | **Clean.** No ERROR/Traceback in the 20 minutes after deploy; the only grep match was the literal `errors=0` inside an INFO capabilities-refresh line. | `kubectl logs deploy/tusker-gateway --since=20m`. |
| Missing gateway alerts | **Present.** Six rules: scrape down, restart storm, OOM-killed, memory near limit, plus storage/node rules. | `k8s/prometheus-rules.yaml:112-141`. |

## Real gaps, ranked by blast radius

### 1. Postgres is an unbacked single point of failure

`k8s/gateway-postgres.yaml` declares a **Deployment with `replicas: 1`** on a
10 GiB Longhorn RWO PVC (`tusker-gateway-postgres-data`). There is no backup
CronJob, no `pg_dump`, and no Postgres-specific alert in
`k8s/prometheus-rules.yaml`.

After the DB-backed config migration the gateway's persistent state is entirely
in this one database:

- config store: `tusker_config_pools`, `tusker_config_providers`,
  `tusker_config_client_keys`, `tusker_config_oauth_credentials`,
  `tusker_config_meta` (`config_store.py:128-170`);
- integrity-chained audit log (`docs/audit-database.md`);
- OAuth credentials, encrypted, with compare-and-swap rotation
  (`config_store.py:1280-1340`);
- quality scores, capability records, cooldowns, provider usage, turn captures.

Losing the PVC therefore loses the live configuration, the audit trail and
every rotated OAuth token — not just request history. The gateway degrades to
`state_store.degraded=true` but cannot reconstruct authoritative state.

**Proposed work** (no code risk, highest value):

1. Nightly `pg_dump` CronJob writing to a separate PVC or object storage, with
   retention and an integrity check (`pg_restore --list` at minimum).
2. A documented, periodically exercised restore procedure. A backup that has
   never been restored is not a backup.
3. Prometheus alerts: `pg_up == 0`, connection count near `max_connections`,
   disk usage above 80 %, backup job not succeeding within 26 h.
4. Only then consider a replicated Postgres (patroni/streaming). A backup is
   the cheaper first step and covers operator error, which replication does not.

### 2. Retention exists in code but is not scheduled

Two independent findings.

**2a. `turn_captures` dominates the database.** Live table sizes:

| Table | Rows | Size |
|---|---|---|
| `turn_captures` | 4,385 | **722 MB** |
| `model_capability` | 34,458 | 17 MB |
| `model_events` | 28,769 | 9.9 MB |
| `audit_events` | 17,818 | 20 MB |

Average captured request body is **779,986 bytes** (~780 KB), max 65,536 for
the response body. The 7-day TTL is `TUSKER_TURN_CAPTURE_TTL_SECS` (default
604800, `turn_capture.py:104`), but the prune runs **only inside `capture()`**
(`turn_capture.py:394-400`), gated by `_PRUNE_INTERVAL_SECS`. Expired rows are
therefore removed only when a *new* turn is captured. A traffic lull or a
capture stall leaves expired rows indefinitely. At the time of inspection 14
rows were already expired.

**2b. Audit retention is never applied.** `AuditLogger.prune()`
(`audit.py:394-422`) deletes events older than `TUSKER_AUDIT_RETENTION_DAYS`
(default 90) while preserving the chain anchor. A repo-wide search finds
**no caller**. `docs/audit-database.md:39-43` describes the 90-day window as
active behaviour; in practice `audit_events` grows without bound.

**Proposed work:**

1. Call both `TurnCaptureStore.prune()` and `AuditLogger.prune()` from the
   existing maintenance loop (`tusker_gateway/maintenance.py`) on a daily
   cadence, with the same best-effort error handling used elsewhere.
2. Regression tests that assert an expired row is removed with no new
   capture, and that the audit chain anchor stays verifiable after pruning.
3. Decide whether 7 days of full request bodies is worth 722 MB. If not,
   lower `TUSKER_TURN_CAPTURE_MAX_REQUEST_BYTES` (currently 4 MiB) or store
   captures in a separate database/PVC so a growth spike cannot fill the
   config store's volume.

### 3. Gateway HA is unblocked but not yet safe for OAuth

The gateway is now genuinely stateless with respect to the filesystem:

- `k8s/deployment.yaml:832-838`: `tusker-home` is an `emptyDir`; the comments
  state all mutable state moved to PostgreSQL including the audit chain.
- Live check: `storage_mode=postgres`, DSN set, `state_store.degraded=false`,
  `recovery_count=0`.
- `strategy: RollingUpdate` with `maxSurge: 1, maxUnavailable: 0` is already
  in place, and the last deploy kept the old pod serving until the new one was
  Ready.
- `replicas: 1` is the only thing preventing HA today.

**The blocker:** the Codex token rotator has no cross-process coordination.
The encrypted DB persistence is a compare-and-swap
(`config_store.py:1309-1340`), but the CAS runs *after* the upstream OAuth
refresh (`passthrough.py:1771-1830`). Two replicas can both decide a token is
near expiry, both consume the same single-use refresh token upstream, and one
then hits `refresh_token_reused` — classified as a **permanent** 24 h cooldown
(`passthrough.py:994-998`). The existing mitigation,
`_maybe_adopt_from_store` (`passthrough.py:1143-1180`), reads the DB before
refreshing but is inherently racy: both replicas can read the same stale
refresh token before either writes.

**Proposed work, in order:**

1. Add a Postgres advisory lock (e.g. `pg_advisory_xact_lock` keyed by provider
   and credential identity) around the adopt-then-refresh-then-persist
   sequence, so only one replica performs an upstream refresh at a time.
2. Regression test with two rotators sharing one DB: both request a token near
   expiry, exactly one refreshes, the other adopts, and neither marks the
   credential permanently failed.
3. Then raise `replicas` to 2 in `k8s/deployment.yaml` and verify a rolling
   restart plus a Codex refresh under load.
4. Consider the same for the two worker Deployments; they are also
   `replicas: 1`, but they hold CLI sessions and need separate analysis.

### 4. Small redundancy introduced by the context-advisory change

`_call_with_pool_fallback` computes `prompt_estimate` at
`endpoints.py:5247`. `_context_advisory_headers` then recomputes it via
`context_advisory_headers` for every response path. Measured cost: **0.61 ms**
per recompute on a 760k-token prompt — negligible relative to a ~5 s request,
but avoidable.

**Proposed work:** thread the already-computed `prompt_estimate` into
`context_advisory_headers(prompt_estimate=...)` at the chat response sites.
Fold this into whichever of the above changes touches `endpoints.py` next;
it does not warrant its own deploy.

## Suggested order

1. Postgres backup + alerts — highest blast radius, no runtime code change.
2. Schedule the two prunes — small code change, fixes a measured leak.
3. Refresh advisory lock, then gateway `replicas: 2`.
4. Thread the estimate through (cleanup, folds into #2 or #3).

## What would change this assessment

- A Postgres failover/backup already running outside this repo (none found in
  the cluster's CronJobs: only `tusker-cli-drift-check` and the wildduck
  backup-verify jobs referenced in `prometheus-rules.yaml`).
- A cluster-autoscaler or operator that manages replica counts independently
  of the manifests (none found).
- A billed-token A/B showing RTK changes cost materially: the counters in
  `tusker_rtk_bytes_saved_total` are bytes, not tokens — see
  `docs/context-efficiency.md`.

## Implementation record (2026-11-25)

All four proposed items were implemented in the PR following this assessment.
`TODO.md` marks the work complete; the mapping from finding to change is:

| Finding above | Change |
|---|---|
| Postgres unbacked, no alerts | `k8s/postgres-backup.yaml` (nightly `pg_dump` to a separate PVC, gzip integrity check, 7-dump retention, restore procedure in comments); five Postgres/backup alerts added to `k8s/prometheus-rules.yaml`. |
| `turn_captures` prune only on write; audit prune never called | `tusker_gateway/retention.py` + daily call from the maintenance loop. |
| Codex refresh race across replicas | `tusker_gateway/oauth_refresh_lock.py` + `ConfigStore.oauth_refresh_advisory_lock()` (bounded 30 s `pg_try_advisory_lock`, fail-open with process-local fallback) around adopt → refresh → CAS persist. |
| `replicas: 1` | `k8s/deployment.yaml` now sets `replicas: 2`; the stale RWX rollout comment was corrected. |
| Duplicate prompt estimate | Chat and Responses handlers compute one estimate per request and pass `prompt_estimate=` to the advisory headers. |

Verification: `tests/test_resilience_followups.py` (12 tests, 6 of which fail
against the unfixed tree), full offline suite, ruff, `py_compile`, YAML parse.
Live verification after deploy is recorded in the deployment handoff below.

### Live verification after deploy (2026-11-25)

Deployed in two steps: `b891e74f…` (image `sha256:2104be59…`) for the feature
set, then `62acb03fec55d02d8330a022855f2226ce1c44e6` (image
`sha256:89a1529c4faecea2b8543642876f7767cc9e803a820087b657ba9878b6a16f50`)
for the three defects that first deploy exposed.

| Check | Result |
|---|---|
| Gateway replicas | 2 pods, both `1/1 Running`, 0 restarts, both report commit `b891e74f` in `/health`. |
| Workers | `tusker-claude-worker` and `tusker-opencode-worker` both `1/1 Running` on the new digest. |
| Provenance | `k8s/verify-provenance.sh` → **OK: all tracked manifests, live Deployments, running pods and /health agree** (with 2 replicas). |
| Retention module | `run_retention_prune` importable and callable in both replicas. |
| Lock key | Stable per credential and namespaced (`0x5455534B` = "TUSK") in both replicas. |
| Backup CronJob | `k8s/postgres-backup.yaml` applied: PVC `Bound` (10 GiB), CronJob `17 3 * * *`, `concurrencyPolicy: Forbid`. |
| Backup execution | Manual run from the CronJob: **Complete 1/1 in 6m11s**, wrote a **505 MB** dump; gzip integrity check passed. |
| Alerts | 11 gateway alerts live, including the 5 new Postgres/backup ones. The live-only `tusker-kilo-worker` group was preserved (the manifest does not contain it). |

**Two defects found and fixed during live verification** — both would have
shipped broken:

1. **The advisory lock never engaged.** `ConfigStore.oauth_refresh_advisory_lock`
   was a sync `@contextmanager` but the caller used `async with`, raising
   `TypeError: '_GeneratorContextManager' object does not support the
   asynchronous context manager protocol`. The fail-open path swallowed it
   (auth kept working via the process-local lock), so the cross-process
   protection was silently absent — HA would not have been safe. Caught in the
   live startup log. Fixed by making it an `@asynccontextmanager`; the
   regression test `test_refresh_lock_guard_works_with_the_real_store_callback`
   fails against the sync version.
2. **The backup could not reach Postgres.** The namespace has
   `default-deny-ingress`, and `allow-tusker-gateway-postgres` only listed the
   gateway and state-migration pods. The dump hung with a 0-byte file until
   `app: tusker-gateway-postgres-backup` was added to the policy (manifest and
   live).

Both fixes are in `k8s/gateway-postgres.yaml` and the gateway source. A third
defect — the missing `tusker-gateway-postgres-config` ConfigMap, which left the
state pod in `Init:0/1` and turned fail-closed audit writes into a 503 for
every caller — is recorded in
`docs/incidents/2026-11-25-postgres-configmap-audit-503.md`; `k8s/deploy.sh`
now applies that ConfigMap.

### Final live state (2026-11-25, after the fixes)

| Check | Result |
|---|---|
| Running image | `sha256:89a1529c…` on the gateway and both workers. |
| Gateway pods | 2 replicas, `1/1 Running`, 0 restarts. |
| Postgres | `1/1 Running`, 0 restarts. |
| Advisory lock | **Zero** `advisory lock unavailable` errors since the rollout, and `pg_try_advisory_lock` + `pg_advisory_unlock` both confirmed executing in the live image. |
| Audit errors | Zero `audit_unavailable` / `audit write exhausted` since the rollout. |
| Smoke | `{"ok": true, "http_status": 200}`. |
| Provenance | `k8s/verify-provenance.sh` → OK across manifest, Deployment, pods and `/health`. |
