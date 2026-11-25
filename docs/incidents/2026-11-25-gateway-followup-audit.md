# Gateway follow-up audit (2026-11-25)

A post-resilience-release audit of the gateway source, the Kubernetes
manifests and the deployment scripts. Everything below was verified against
the checked-out tree or the live cluster; nothing is speculative.

The resilience release (`b891e74`, `62acb03`, `9cc43f9`, `2df6e83`) is
recorded in `docs/resilience-performance-assessment.md` and
`docs/incidents/2026-11-25-resilience-release-defects.md`. This audit covers
what that release and the earlier work left behind.

## Fixed in this release

### 1. The logical Postgres backup was never applied by the deploy script

`k8s/postgres-backup.yaml` defines the backup PVC and the nightly `pg_dump`
CronJob, but `k8s/deploy.sh` applied only the Longhorn snapshot manifest. A
deployment from the documented procedure produced a cluster with volume
snapshots and **no logical backup**, while the deploy script still reported
success.

The nightly dump is the only backup of the state store that survives a
corrupted volume: the snapshot job is volume-level, and the cluster's
`weekly-backup` Longhorn job keeps a single copy up to seven days old.

Fix: `k8s/deploy.sh` now applies `k8s/postgres-backup.yaml` alongside the
other state-plane manifests. Regression test:
`tests/test_gateway_followup_fixes.py::test_deploy_script_applies_the_logical_backup_manifest`.

### 2. The backup CronJob could publish a truncated dump as a success

The dump command ran `pg_dump ... | gzip -9 > "${tmp}"` without
`pipefail`. The pipeline's exit status is gzip's, so a `pg_dump` that died
partway through still produced a valid gzip file that passed the size check
and the `gzip -t` integrity check. The CronJob then reported success and the
`TuskerGatewayBackupStalled` / `TuskerGatewayBackupNeverRan` alerts stayed
quiet while the newest published dump was unusable.

Fix: `set -o pipefail` in the dump script, so a failed `pg_dump` fails the
job before anything is published. Regression test:
`tests/test_gateway_followup_fixes.py::test_backup_command_fails_closed_when_pg_dump_fails`.

### 3. The Anthropic surface bypassed the direct-route cooldown gate

PR #85 added the cooldown gate to explicit `provider/model` routes in
`POST /v1/chat/completions`. `POST /v1/messages` has its own dispatch path
(`tusker_gateway/anthropic_adapter.py`) and never called
`cooldown_gate_error()`, so a quarantined route kept receiving Anthropic
traffic.

Fix: the Anthropic direct branch applies the same gate, with the same
loopback-qualification-probe exemption. A cooldown now surfaces as 429 with
the enforced `Retry-After` (503 for provider-wide quota providers, matching
the OpenAI surface). Regression tests:
`tests/test_gateway_followup_fixes.py::test_anthropic_direct_route_in_cooldown_does_not_reach_upstream`
and `...::test_anthropic_direct_route_outside_cooldown_still_dispatches`.

### 4. A rotator rebuilt by a config reload lost the cross-process refresh lock

`ConfigRuntime._rebuild_rotators()` passed `persist_credentials` and
`load_credentials` to a newly constructed `CodexTokenRotator`, but not
`refresh_lock`. A rotator created that way fell back to the process-local
lock, so the two gateway replicas could consume the same single-use Codex
refresh token — the exact `refresh_token_reused` → 24 h permanent cooldown
failure the advisory lock exists to prevent.

Fix: `_rebuild_rotators()` now passes the store's `oauth_refresh_lock`
callback. Regression test:
`tests/test_gateway_followup_fixes.py::test_rebuild_rotators_passes_the_refresh_lock`.

### 5. The SQLite advisory-lock path provided no mutual exclusion

`ConfigStore.oauth_refresh_advisory_lock()` created a **new**
`threading.Lock()` on every call for the non-PostgreSQL path, so no two
callers ever contended on the same object. Because the callback succeeded,
`RefreshLock.guard()` never reached its own keyed local-lock fallback
either, so two rotators sharing a SQLite store could run adopt → refresh →
persist concurrently.

Fix: one process-wide mutex per advisory key
(`tusker_gateway/config_store.py::_sqlite_refresh_lock`), bound to the
running event loop. Regression test:
`tests/test_gateway_followup_fixes.py::test_sqlite_advisory_lock_serializes_two_callers`.

### 6. The provider-health payload omitted capacity quarantines and invented providers

`CooldownTracker.snapshot()` reports three kinds of cooldown — model-scoped,
provider-scoped and **capacity-group-scoped** — but `provider_health_payload()`
only consumed the first two. A `cli` group quarantine (both CLI worker
providers) was enforced by routing yet invisible to `/v1/provider-health`,
which could report every provider as ready.

The same builder also inserted the usage-ledger keys directly into the
provider map. That ledger is keyed by the *logical capacity group*, so
`opencode-cli` and `claude-code-cli` usage appeared as a synthetic
`provider: "cli"` row, distorting the provider count and attribution.

Fix: the payload now reports providers and capacity groups separately, marks
every provider behind a quarantined group as not ready, and attributes shared
group usage to the group. Regression tests:
`tests/test_gateway_followup_fixes.py::test_provider_health_reports_capacity_group_quarantine`
and `...::test_provider_health_does_not_report_groups_as_providers`.

### 7. Scheduled retention depended on unrelated switches

`run_retention_prune()` was called only from the qualification maintenance
loop, and only when `TUSKER_QUALITY_EVENT_PRUNE_ENABLED` was on. A
deployment with qualification maintenance disabled — or an operator turning
the unrelated quality-event pruning off — got no capture pruning and no audit
pruning, so the documented 7-day capture window and 90-day audit window were
not enforced.

Fix: a dedicated `retention_maintenance_loop` (`tusker_gateway/maintenance.py`)
runs on its own daily clock and is started unconditionally in
`tusker_gateway/app.py`. Regression tests:
`tests/test_gateway_followup_fixes.py::test_retention_task_starts_without_qualification_maintenance`
and `...::test_retention_loop_prunes_without_qualification_enabled`.

### 8. The Grafana admin password was committed to the repository

`k8s/prometheus-values.yaml` carried `grafana.adminPassword:
"tusker-grafana"`, and the live `monitoring/monitoring-grafana-admin` Secret
still uses that value. Grafana is reachable through the public ingress at
`https://ai.tusker.net.au/grafana`, so the credential was published.

Fix: the values file now points the chart at the existing Secret
(`grafana.admin.existingSecret`) and carries no credential. **The live
Secret's password must be rotated** — that is a cluster change and was not
performed as part of this release.

## Open, latent

These are real defects but are not reachable in the current production
configuration. They are recorded here so they are not re-discovered later.

### 9. Circuit-breaker counters use a non-atomic read-modify-write

`CircuitBreaker.record_failure()` reads the row, computes the incremented
counters in Python and writes them back in a second statement
(`tusker_gateway/circuit_breaker.py:384-460`). Two concurrent failures can
both read `consecutive_failures = 1` and both write `2`, so the second
failure is lost and a breaker with a threshold of 3 stays closed.

**Blast radius: latent.** The breaker is disabled in production
(`TUSKER_CIRCUIT_ENABLED=false` in `k8s/deployment.yaml`). Fixing it needs
an atomic `UPDATE ... SET counter = counter + 1` (or a per-route lock) and
a decision about the half-open probe accounting; it is not a release-sized
change.

### 10. Half-open multi-probe semantics contradict the module documentation

With `half_open_max_probes > 1`, the first successful probe closes the
breaker and resets the counters, so a later failing probe no longer reopens
it; a late success after a failed probe can close an open breaker. The
module documentation says "if all admitted probes succeed, full traffic
resumes; if any fails, the breaker reopens". Same latent status as #9: the
default policy uses one probe, and the breaker is disabled in production.

### 11. Prometheus rules for Postgres evaluate per pod, not per deployment

`TuskerGatewayPostgresNoReadyPod` uses
`kube_pod_status_ready{...} == 0`, which fires for any single non-ready pod
even when another is ready. The Deployment is a single replica today, so the
rule and its annotation agree in practice; a future scale-out would make it
noisy.

### 12. Backup and drift-check images are not digest-pinned

`k8s/postgres-backup.yaml` and `k8s/cli-drift-cronjob.yaml` use mutable tags
(`postgres:16-alpine`, `python:3.11-slim`). The gateway and both workers are
digest-pinned and covered by `k8s/verify-provenance.sh`; these two jobs are
not. Pinning them means tracking digests for images that are rebuilt
upstream, so it is an operator policy decision rather than a defect.

## Verification

| Check | Result |
|---|---|
| `tests/test_gateway_followup_fixes.py` | 10 tests; 9 fail against the unfixed tree |
| Full offline suite | 2322 passed, 8 skipped |
| `ruff check` | clean on the changed modules |
| `python -m py_compile` | clean on the changed modules |
| `bash -n k8s/deploy.sh` | clean |
| YAML parse of the changed manifests | clean |
