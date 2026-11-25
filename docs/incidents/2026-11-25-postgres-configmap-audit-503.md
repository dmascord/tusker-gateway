## 2026-11-25 — Missing Postgres ConfigMap failed requests closed with 503 audit_unavailable

### Symptom

A caller received the gateway's fail-closed audit error for
`req_3ed57e8324994ea8`:

> The gateway could not persist the required audit record for this request. Retry shortly or contact the gateway operator with the request ID: req_3ed57e8324994ea8.

The client's retry layer then exhausted three attempts against the same
failure, reporting `503 no available server` ("Retry budget exhausted after 3
retries" / "Retry failed after 3 attempts").

This is not a provider fault. Since the audit chain moved to PostgreSQL
(2026-11-24), `TUSKER_AUDIT_FAIL_CLOSED=true` makes an unavailable state store
a request-level failure: `AuditWriteError` is rendered as `audit_unavailable`
with a 503. A storage/control-plane outage therefore presents to callers as a
generic "no available server".

### Timeline (UTC)

| Time | Event |
|---|---|
| 00:01 | Gateway pods created on image `sha256:2104be59…` (commit `b891e74`); no ConfigMap `tusker-gateway-postgres-config` in namespace `hermes`. |
| 00:10 | Postgres deployment rolled to ReplicaSet `tusker-gateway-postgres-65975677fd`; the new pod `…-mgrk9` was scheduled. |
| 00:11–00:36 | Postgres pod stuck at `Init:0/1`, `PodInitializing`. Kubelet repeated `MountVolume.SetUp failed for volume "postgres-config" : configmap "tusker-gateway-postgres-config" not found`. |
| 00:13–00:36 | Both gateway pods crash-looped: readiness/liveness `/ready` and `/health` timed out, containers were killed (exit 137). |
| 00:32–00:36 | Gateway logs show `psycopg.pool ... connection to server at "10.101.190.90", port 5432 failed: Connection refused`, followed by repeated `audit append retry …/4 after StorageUnavailableError` and `audit write exhausted retries`. |
| 00:37 | ConfigMap restored; postgres container started 00:37:28Z and became Ready. |
| 00:38–00:41 | Gateway rollout reached 2/2; no new audit errors; production smoke returned HTTP 200. |

### Root cause

`k8s/gateway-postgres.yaml` mounts a ConfigMap named
`tusker-gateway-postgres-config` at `/etc/postgresql/override/override.conf`,
and the `postgres-config-include` init container appends an `include_if_exists`
line to `postgresql.conf`. The ConfigMap is defined in
`k8s/postgres-config.yaml`, but that file is **not applied anywhere in the
normal deploy path**: `k8s/deploy.sh` applies `pvc.yaml`, `config.yaml`, the
three workloads, `service.yaml` and `ingressroute.yaml`, while PostgreSQL is
managed separately. The ConfigMap was therefore absent from the cluster, so
the state pod could not mount its volume and never started.

With no database on `10.101.190.90:5432`, every audit append raised
`StorageUnavailableError`. Fail-closed semantics turned that local
control-plane outage into `503 audit_unavailable` for all callers, and the
health/readiness checks — which depend on the state store — caused the gateway
pods to be killed repeatedly. This is the same client-visible failure family as
the 2026-11-24 NFS flock incident (`docs/incidents/2026-11-24-audit-flock-eio.md`),
but with a different root cause: a missing ConfigMap rather than a storage lock.

### Remediation

1. Immediate: recreated the ConfigMap from its committed manifest:
   `kubectl apply -f k8s/postgres-config.yaml`. Kubelet retried the mount, the
   init container ran, and the postgres pod became Ready.
2. Follow-up (this change): `k8s/deploy.sh` now applies
   `k8s/postgres-config.yaml` in the standard deploy path before the workload
   rollout, with a comment explaining the dependency. A regression test in
   `tests/test_deploy_scripts.py` asserts the manifest is applied so a future
   edit cannot silently drop it again.

### Verification

- `configmap/tusker-gateway-postgres-config created`; present in `hermes`.
- `deployment.apps/tusker-gateway-postgres`: 1/1 available, pod Ready.
- `deployment.apps/tusker-gateway`: 2/2 available, rollout successful; no
  further readiness/liveness failures.
- Gateway logs since recovery: no `StorageUnavailableError`, no
  `audit write exhausted retries`, no connection-refused entries.
- `./k8s/smoke_production.sh`: HTTP 200.
- `bash -n k8s/deploy.sh` clean; `pytest tests/test_deploy_scripts.py` green.

### Not changed

The 503 response text and `audit_unavailable` code are correct and were not
touched — they accurately name a local audit-persistence failure. The
client-side "503 no available server" wording comes from the retrying client,
not the gateway. We also did not change fail-closed semantics: making audit
writes advisory would hide real integrity failures; the fix is to ensure the
database dependency is deployed, not to weaken the guarantee.

The specific request ID `req_3ed57e8324994ea8` was not found in retained pod
logs (the gateway pods restarted several times during the incident), but the
symptom, timing and error path match the verified audit/PostgreSQL outage
exactly.
