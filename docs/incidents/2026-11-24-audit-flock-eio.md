## 2026-11-24 — NFS flock EIO on the shared audit log failed requests closed

### Symptom

OMP streaming requests returned an in-band SSE error, e.g.:

> Upstream provider error: required audit persistence failed. Request ID: req_9e48dfe2c9155c3a.

Affected IDs in the first report: `req_9e48dfe2c9155c3a`,
`req_80ff49093b84f261`, `req_31d576e2cff6b4c5`. Failures began ~02:25Z and
continued past 02:36Z. The same fault returned the gateway's 503
`audit_unavailable` JSON to non-stream callers — including the Hindsight memory
service, which routes through the gateway, so memory recall went down with it.

### Root cause

The audit log is `/home/tusker/.hermes/audit.jsonl` on the `tusker-home-rwx`
Longhorn RWX (NFSv4) PVC, shared by the gateway and both CLI workers. Every
append takes an exclusive `flock` (`tusker_gateway/audit.py:165`). The
share-manager's NFS lock state went stale, and that `flock` began failing with
`OSError: [Errno 5] Input/output error`:

```
File ".../audit.py", line 165, in _append
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
OSError: [Errno 5] Input/output error
...
File ".../endpoints.py", line 3688, in _early_stream
    _policy_evaluation_event(...)
File ".../endpoints.py", line 2590, in _policy_event
    writer(event)              # write_sync
File ".../audit.py", line 263, in write_sync
    raise AuditWriteError("required audit persistence failed")
```

With `TUSKER_AUDIT_FAIL_CLOSED=true` the failed tool-policy audit event aborted
the healthy stream. `_public_stream_error` then had no dedicated arm for
`AuditWriteError`, so the local control-plane failure was wrapped in the
generic "Upstream provider error" text and sent operators to chase provider
health. A scratch write + fsync on the same mount succeeded throughout, which
isolated the fault to the NFS lock rather than capacity or permissions.

### Remediation

1. Immediate mitigation: `kubectl set env deployment/tusker-gateway
   TUSKER_AUDIT_FAIL_CLOSED=false` (fast rollout, no rebuild). Client-visible
   audit errors stopped; audit events were still attempted, just not fatal.
2. Root-cause fix: deleted the stale
   `share-manager-pvc-2df570ce-…` pod in `longhorn-system`; Longhorn recreated
   it and it reached Ready. A blocking `flock(LOCK_EX)` on the audit file went
   from immediate EIO to success, and the audit file resumed advancing.
3. Restored `TUSKER_AUDIT_FAIL_CLOSED=true` once the volume was healthy — zero
   audit errors afterwards.

### Code fix (commit `99931eecfc98cb11bf9235e865e000a4d30820e6`)

- `tusker_gateway/endpoints.py`: `_public_stream_error` now has an
  `AuditWriteError` arm — "The gateway could not persist the required audit
  record …" with code `audit_unavailable`, no "Upstream provider error" prefix.
- `tusker_gateway/audit.py`: `write_sync` now spaces its four retries with a
  short escalating backoff (0.02/0.04/0.08s). Previously all four attempts ran
  in the same millisecond, so any sub-100ms shared-volume blip still failed
  closed.
- Regression tests in `tests/test_chat.py` and
  `tests/test_audit_concurrency.py`.

### Verification

- `pytest tests/ --ignore=tests/test_passthrough_providers.py`: 2226 passed,
  8 skipped.
- Live smoke (`k8s/smoke_production.sh`) 200; `/health` commit
  `99931eecfc98cb11bf9235e865e000a4d30820e6`; fail-closed back to `true` with
  zero audit errors and a continuously advancing audit file.
- Hindsight memory service recovered, confirming the shared-volume fault was
  cleared.
- Deployed image digest
  `sha256:cc8ea30ae4370195d3070e21ff8c3e04065975e161fecd8434893619322d295c`;
  `k8s/verify-provenance.sh` OK across all three workloads.

### Follow-up — implemented

The shared-file lock path has since been removed entirely: audit events now
append to the shared PostgreSQL state store with a serialized row lock. See
`docs/audit-database.md` for the design, migration tool, and retention policy.
