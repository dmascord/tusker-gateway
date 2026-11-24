# NFS / RWX retirement (2026-11-24)

The gateway stack no longer uses NFS. The Longhorn `longhorn-rwx`
StorageClass and `tusker-home-rwx` PVC existed only so a `RollingUpdate`
surge pod and the old pod could share a filesystem. Once all mutable state
moved to PostgreSQL — including the audit chain in the previous change —
that constraint disappeared.

## What was removed

- `k8s/deployment.yaml`, `k8s/worker-opencode.yaml`, `k8s/worker-claude.yaml`:
  the `tusker-home` volume changed from `persistentVolumeClaim:
  tusker-home-rwx` to `emptyDir: {}`.
- `k8s/deploy.sh`: no longer applies `k8s/pvc-rwx.yaml`.
- Cluster: the `tusker-home-rwx` PVC and `longhorn-rwx` StorageClass were
  deleted; Longhorn reaped the share-manager and the RWX volume.

`k8s/pvc-rwx.yaml` and `k8s/longhorn-rwx-sc.yaml` remain in the tree only as
historical records and are marked deprecated.

## Why pod-local storage is safe

- Gateway: no open files and no writes on the old share after audit moved to
  PostgreSQL. Credentials are DB-authoritative (`config_db_credentials_
  authoritative`) with env-var pools; `auth.json` is a legacy fallback only.
- Workers: the live `.claude.json` lives at `/home/tusker/.claude.json`,
  outside the old mount. The share held only `.claude.json` backups, plugin
  caches, sessions and project dirs — all reproducible CLI state.

## Historical audit evidence

Before retiring the PVC, the 776 MB JSONL log was copied to the retained RWO
`tusker-home` volume as `audit-archive-20261124.jsonl` and verified in place:

```
records=1059533 valid=true
```

It remains independently verifiable with:

```bash
python -m tusker_gateway.tools.verify_audit \
    /home/tusker/.hermes/audit-archive-20261124.jsonl --json
```

(from a pod mounting `tusker-home`). The live audit chain continues in
PostgreSQL; see `docs/audit-database.md`.

## Verification

- After the rollout, no pod in the `hermes` namespace reported any NFS
  mount (`mount | grep 'type nfs'` empty across all pods).
- `tusker-gateway`, `tusker-opencode-worker` and `tusker-claude-worker`
  all Ready on the new digest; `/health`, `/ready` and the authenticated chat
  SSE smoke passed.
- `verify_audit --database --json` reported `valid=true` with records
  accumulating from live traffic.
- No audit errors in the gateway logs after the rollout.
