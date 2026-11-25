# Investigation: the three defects found by deploying the resilience release (2026-11-25)

The resilience release (`b891e74` → `62acb03`) was verified by deploying it.
Deploying surfaced three defects that no offline test had caught. Each is
recorded here with its root cause, why the existing safeguards missed it, and
the state of the fix. Two further defects were found while investigating these
(§4) — both in code introduced by the release itself.

Everything below was verified against the live cluster or by reproducing the
failure locally.

---

## Defect 1 — the cross-process OAuth advisory lock never engaged

### Symptom

The live startup log of both gateway replicas:

```
WARNING [tusker_gateway.oauth_refresh_lock] oauth refresh advisory lock
unavailable provider=openai-codex identity_fp=rt.1.AAC2O9h
Traceback (most recent call last):
  File "/opt/tusker-gateway/tusker_gateway/oauth_refresh_lock.py", line 70, in guard
    async with self._acquire(key):
TypeError: '_GeneratorContextManager' object does not support the asynchronous
context manager protocol
```

### Root cause

`ConfigStore.oauth_refresh_advisory_lock` was decorated with the **sync**
`@contextmanager`, but `RefreshLock.guard` enters it with `async with`. The
sync generator cannot be entered asynchronously, so the call raised `TypeError`
before taking any lock.

`RefreshLock.guard` catches `Exception` and falls open to a process-local
lock. That is why **the failure was invisible**: auth kept working, and the
one mechanism whose absence was supposed to be loud was silent.

### Why the safeguards missed it

- The unit test used a hand-written fake async lock
  (`_AsyncLock` in `tests/test_resilience_followups.py`), so it exercised
  `RefreshLock` against the *shape* it expected, not against the real
  `ConfigStore` method. The two contracts were never connected.
- The fail-open path is by design non-fatal, so no exception surfaced to the
  caller.
- No metric or alert existed for "lock unavailable" — only a WARNING log line.

### Impact

`replicas: 2` was live while the cross-process protection was **not**. Two
replicas could still consume the same single-use Codex refresh token; the
loser gets `refresh_token_reused`, which is classified as a *permanent* 24 h
cooldown (`passthrough.py:_PERMANENT_REFRESH_ERROR_CODES`). That is the exact
failure the change was meant to prevent.

### Fix state — **fixed and verified**

- `oauth_refresh_advisory_lock` is now `@asynccontextmanager`
  (`config_store.py:1322`).
- The test was rewritten to drive the **real** store method
  (`test_refresh_lock_guard_works_with_the_real_store_callback`); it fails
  against the sync version.
- Live: zero `advisory lock unavailable` errors since the rollout, and
  `pg_try_advisory_lock` + `pg_advisory_unlock` both confirmed executing
  inside the deployed image.

---

## Defect 2 — the backup could not reach Postgres

### Symptom

The first manual backup run produced a 0-byte file and sat there:

```
-rw-r--r--  1 root ping  20    tusker_gateway-20261125T000632Z.sql.gz.partial
-rw-r--r--  1 root ping   0    tusker_gateway-20261125T000848Z.sql.gz.partial
```

`pg_dump` never connected; the job just hung until the deadline.

### Root cause

The `hermes` namespace has a namespace-wide default deny:

```yaml
# default-deny-ingress
spec:
  podSelector: {}          # selects EVERY pod in the namespace
  policyTypes: [Ingress]
```

Ingress to Postgres is allowed only by `allow-tusker-gateway-postgres`, whose
`from` list named the gateway pods and the state-migration pods. The new
backup pod (`app: tusker-gateway-postgres-backup`) was not in that list, so
its packets to port 5432 were dropped. `pg_dump` blocked on connect rather
than failing fast, so the job looked alive.

### Why the safeguards missed it

- The backup manifest was written and applied without checking the
  namespace's network policy.
- `pg_dump` connecting to a Service name that resolves correctly but is
  firewalled produces a *hang*, not an error — the job's `activeDeadlineSeconds`
  was the only backstop.
- No policy test asserts that a new pod's egress to Postgres is permitted.

### Fix state — **fixed and verified**

- `k8s/gateway-postgres.yaml` now lists
  `app: tusker-gateway-postgres-backup` in the allow policy.
- Live: the manual job completed **1/1 in 6m11s** and wrote a **505 MB**
  dump that passed the gzip integrity check.

### Related gap found during this investigation

`default-deny-ingress` uses `podSelector: {}`, so **every** pod in `hermes` is
covered by it. Any new workload in the namespace needs an explicit allow
policy for every port it must receive, and the repository has no test that
would catch a missing one. The gateway's own manifests are all present in the
cluster (verified individually), so this is a forward-looking risk rather
than a current break.

---

## Defect 3 — the Postgres ConfigMap was never applied (production outage)

### Symptom

Callers received the gateway's fail-closed audit error, and both gateway pods
crash-looped for ~25 minutes:

```
The gateway could not persist the required audit record for this request.
Retry shortly or contact the gateway operator with the request ID:
req_3ed57e8324994ea8
```

The client's retry layer then reported `503 no available server` after
exhausting three attempts. Gateway logs showed:

```
psycopg.pool ... connection to server at "10.101.190.90", port 5432 failed:
Connection refused
audit append retry …/4 after StorageUnavailableError
audit write exhausted retries
```

### Root cause

`k8s/gateway-postgres.yaml` mounts a ConfigMap named
`tusker-gateway-postgres-config`, and `k8s/postgres-config.yaml` defines it.
**That file was not applied anywhere in the normal deploy path.** It was
committed in `de45e01` with the note *"Operator-applied (not part of the
gateway deploy.sh). Apply once with kubectl apply -f"* — an instruction that
was never executed in this cluster.

The outage was triggered when `k8s/gateway-postgres.yaml` was applied
during this release: the Deployment rolled, the new pod could not mount the
missing ConfigMap, and the state store never came up.

With `TUSKER_AUDIT_FAIL_CLOSED=true`, an unavailable state store becomes a
request-level failure, so a local control-plane outage presented to every
caller as a generic 503. The readiness/liveness probes depend on the state
store, so both gateway pods were killed repeatedly (3 restarts each).

### Why the safeguards missed it

- `deploy.sh` applied `pvc.yaml`, `config.yaml`, the three workloads,
  `service.yaml` and `ingressroute.yaml`. Postgres was managed separately
  and its two support manifests were never in that list.
- The ConfigMap's absence only manifests when the **Postgres pod is
  recreated** — a plain gateway deploy never noticed.
- `deploy.sh` had no assertion that a mounted ConfigMap exists before
  rolling a workload that depends on it.

### Fix state — **fixed and verified**

- `k8s/deploy.sh` now applies `k8s/postgres-config.yaml` **before** the
  workloads (line 139, before the workload apply loop).
- `tests/test_deploy_scripts.py::test_deploy_script_applies_postgres_configmap`
  asserts it stays in the deploy path.
- Recorded in `docs/incidents/2026-11-25-postgres-configmap-audit-503.md`.

**Honest note:** this outage was caused by my own `kubectl apply -f
k8s/gateway-postgres.yaml` during the release. The missing ConfigMap was a
pre-existing latent defect, but I triggered it.

### Remaining gap in this area

`k8s/gateway-postgres.yaml` is still **not** applied by `deploy.sh`. A fresh
cluster therefore has no Postgres at all, and anyone applying that manifest
by hand can still roll the state store. Applying the ConfigMap first (the
fix) removes the crash-loop, but the deeper fix is to make the Postgres
Deployment part of the managed deploy path, or to add a pre-flight assertion
that every mounted ConfigMap exists before any rollout.

---

## 4. Two further defects found while investigating the above

Both are in code introduced by this release, both are **present in the
deployed image**, and both were found by reasoning about the fail-open path
rather than by the original tests.

### 4a. An exception inside the guarded block corrupts the refresh path

`RefreshLock.guard` wraps the whole `async with self._acquire(key): yield`
in `try/except Exception`. When the **body** raises — which is the normal
outcome of a failed upstream refresh — the exception is caught by that same
`except`, treated as "the lock is unavailable", and the guard body is
**re-entered** through the local-lock fallback.

Reproduced against the real store method:

```
RESULT: caller got RuntimeError: generator didn't stop after athrow()
```

The caller no longer sees the original refresh error; it sees a
`contextlib` internal error. The `yield` is then thrown into the generator a
second time, producing `RuntimeError: generator didn't stop after athrow()`.

Verified present in the deployed image:

```
DEPLOYED BEHAVIOUR: RuntimeError generator didn't stop after athrow()
```

**Impact:** any failed Codex token refresh raises a misleading error instead
of the real cause, and the refresh path is corrupted exactly when it is
already failing. The failure mask also hides the true reason from the
cooldown classification that decides whether the credential is permanently
failed.

**Blast radius today:** latent. No refresh has been attempted in the last 48 h
(tokens are not near their 120 s refresh margin), so it has not fired yet. It
would fire the first time a Codex token comes up for renewal and the upstream
refresh fails.

**Fix:** the fail-open path must only cover *lock acquisition*, never the
guarded body. The `except` has to be scoped to the acquisition step alone.

### 4b. A lock acquired but not yielded is never released

If the store's context manager acquires the advisory lock and then raises
before yielding, the fail-open path swallows that and continues with the
local lock — leaving the **session-level advisory lock held** on that
connection. It is released only when the connection dies, so every other
replica blocks on that credential for as long as the session lives.

Reproduced with a store callback that acquires then raises:

```
advisory acquired: True | released: False
VERDICT: LOCK LEAKED (deadlock risk)
```

**Impact:** a partial failure in the store's lock acquisition can wedge
refresh for that credential across all replicas. The 30 s wait bound means
each request pays 30 s before falling back, so it degrades rather than
hard-fails — but it does not self-heal.

**Fix:** the store's context manager must guarantee release of anything it
acquired, including on the error paths between acquisition and `yield`.

---

## 5. Other findings from this investigation (not defects in this release)

### 5a. The gateway Postgres volume has no 15-minute snapshot protection

`docs/postgres-ha-design.md` declares Phase 1 "shipped" with
*"RPO after Phase 1: ≤15 minutes (snapshot interval)"*, relying on
`k8s/postgres-snapshot-job.yaml`. That manifest was **never applied**, and
it would not have worked anyway:

- It targets `groups: [default]`, but the gateway volume carries
  `backup-target: default` and is covered by the cluster's **`weekly-backup`**
  recurring job (task `backup`, cron `0 3 * * 0`, `retain: 1`).
- The cluster's `daily-snapshot` job (task `snapshot`, groups `[]`) stopped
  covering this volume on 2026-11-12.

Measured live state of `pvc-047d8909-1fe3-4612-9593-f5679e86d9ba`:

| Protection | Actual |
|---|---|
| Snapshots referencing the volume | 8 |
| Newest snapshot | **2026-11-22** (3 days stale at time of check) |
| Coverage | `weekly-backup` only — **one** recovery point, up to 7 days old |
| Retention | `retain: 1` — a single copy, 28-day age limit |

So the real RPO for the database holding the config store, audit chain and
OAuth credentials is **up to a week**, not 15 minutes. The nightly
`pg_dump` added by this release narrows that to a day but is a logical dump,
not a volume-level point-in-time recovery.

**Needed:** apply a Longhorn recurring job that actually selects this volume
(`backup-target: default` / the right group for the job's task), and
correct `docs/postgres-ha-design.md` to state the true RPO.

### 5b. Dead code: the lock enable/disable toggle is never read

`oauth_refresh_lock.load_refresh_lock_from_env()` and the
`TUSKER_OAUTH_REFRESH_LOCK` variable it reads are never called from anywhere.
An operator reading the module would reasonably believe setting
`TUSKER_OAUTH_REFRESH_LOCK=0` disables the lock. It does not.

### 5c. No metric or alert for lock unavailability

The fail-open path emits a WARNING log line and nothing else. Both defect 1
and 4a were invisible to monitoring. A counter
(`tusker_oauth_refresh_lock_unavailable_total`) plus an alert would have
made defect 1 a page rather than a log line I happened to read.

### 5d. The workers run in postgres mode without a DSN

`tusker-opencode-worker` and `tusker-claude-worker` both report
`storage_mode=postgres` with no `TUSKER_STATE_DATABASE_URL`; any state-store
access raises `StorageModeError`. They do not touch the state store today, so
they are not broken, but the mode/DSN mismatch means a future change that
gives a worker state access would fail at runtime rather than at startup.

---

## Verification summary

| Check | Result |
|---|---|
| Defect 1 fix | Zero `advisory lock unavailable` errors live; `pg_try_advisory_lock` + `pg_advisory_unlock` both confirmed executing in the deployed image. |
| Defect 2 fix | Manual backup job completed 1/1 in 6m11s, 505 MB dump, gzip check passed. |
| Defect 3 fix | `deploy.sh` applies the ConfigMap before the workloads; test asserts it; both gateway pods and Postgres stable, zero audit errors since. |
| Defect 4a | **Open** — reproduced locally and confirmed in the deployed image. |
| Defect 4b | **Open** — reproduced locally with a store callback that acquires then raises. |
| 5a | **Open** — the 15-minute snapshot job was never applied and would not match the volume; real RPO is up to a week. |
| 5b, 5c | **Open** — dead toggle, no metric/alert. |
| 5d | **Open** — informational; workers are unaffected today. |

---

## 2026-11-25 01:33Z — working-tree edits discarded during deployment pinning

While pinning the manifests for the direct-route cooldown gate deployment
(PR #86), an assistant ran `git reset --hard origin/main` in this shared
worktree to align with the merged pin commit. That discarded uncommitted edits
to `tests/test_resilience_followups.py`, `tusker_gateway/config_store.py`,
`tusker_gateway/metrics.py`, and `tusker_gateway/oauth_refresh_lock.py`.
The edits were never staged, so git has no object from which to restore them.
This untracked document survived.

The discarded work concerned the open defects recorded above (the advisory
lock failure path, refresh-lock env loading, and the dead
`TUSKER_STATE_DATABASE_URL` toggle for workers). It must be re-applied before
those items can be marked fixed. The deployment that was pinned at the same
time (`1abb247`, image `sha256:133de89d…`) contains only the committed source
revision `659f250` and was built from a `git archive` export, so **no discarded
edit entered the running image**.

Process note: use `git merge --ff-only` or a worktree for a pin commit in a
shared tree; never `git reset --hard` when other sessions have unstaged work.

**Update (01:40Z):** the other session resumed work and has re-applied edits
to `endpoints.py`, `metrics.py`, `oauth_refresh_lock.py`, and `tool_formats.py`
plus a new `tests/test_request_tool_arguments.py`. The list above records the
first loss; those files must be reconciled against the current working tree
before treating the note as a full inventory. `config_store.py` and
`tests/test_resilience_followups.py` were not yet re-applied at that time.

---

## 6. Follow-up work applied (2026-11-25, after the loss note above)

All files named in the loss note have been re-applied and are covered by
regression tests that fail against the unfixed tree.

### Fixed

- **4a — body exceptions masked.** `RefreshLock.guard` now calls
  `__aenter__` in its own `try`, so only *acquisition* failures fail open.
  An exception from the guarded block propagates unchanged and the blocking
  is released by the `finally`. Repro that used to return
  `RuntimeError: generator didn't stop after athrow()` now returns the
  original `RuntimeError: upstream refresh failed`.
- **4b — leaked advisory lock.** `ConfigStore.oauth_refresh_advisory_lock`
  wraps acquisition and the guarded block in one outer `try/finally`, so an
  unlock is attempted whenever the lock was acquired. `RefreshLock` also
  calls `__aexit__` when `__aenter__` fails part-way, which is the only
  release path for a lock taken before the failure.
- **5b — dead toggle.** `load_refresh_lock_from_env()` and the
  `TUSKER_OAUTH_REFRESH_LOCK` env var are removed. The lock is unconditional;
  a variable that cannot disable it is worse than no variable.
- **5c — silent fail-open.** New counter
  `tusker_oauth_refresh_lock_unavailable_total{provider}`, incremented from
  the fail-open path via a registry bound at app startup
  (`bind_metrics_registry`). `provider` is a bounded label.
- **5a — snapshot job could never match.** `k8s/postgres-snapshot-job.yaml`
  now targets a dedicated `gateway-postgres-snapshots` group, and the PVC in
  `k8s/gateway-postgres.yaml` is labelled as the recurring-job source for
  that group. `docs/postgres-ha-design.md` no longer claims a 15-minute RPO
  as shipped state.

### Verified not a defect

- **5d — workers in postgres mode without a DSN.** `tusker_gateway/worker.py`
  and every adapter it dispatches to have no state-store reference at all;
  the worker imports cleanly under the default mode. The mode/DSN mismatch
  cannot fail today. It is recorded as an environment-shape observation, not
  an open defect, and was left unchanged rather than adding a DSN that no
  worker code reads.

### Regression coverage

`tests/test_resilience_followups.py` now includes, each proven failing against
the unfixed tree:

| Test | Fails before because |
|---|---|
| `test_guard_propagates_body_failure_without_rerunning_it` | body error was converted to `generator didn't stop after athrow()` |
| `test_guard_releases_lock_when_acquire_partially_succeeds` | fail-open skipped `__aexit__` on a partially acquired lock |
| `test_guard_counts_fail_open_acquisitions` | no `_metrics_registry` / no counter existed |
| `test_postgres_snapshot_job_targets_the_gateway_volume` | job used `groups: [default]`, which cannot match this PVC |

### Still open

`gateway-postgres-snapshot-15m` has not been applied to the cluster yet;
applying it is a live-cluster change requiring operator authorization.
Until then the true RPO for the gateway database remains the cluster's weekly
Longhorn backup (`retain: 1`, last successful 2026-11-22) supplemented by the
nightly `pg_dump`.
