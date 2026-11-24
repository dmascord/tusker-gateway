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

  **Exception — Claude Code CLI OAuth login.** The CLI keeps its login in
  `/home/tusker/.claude/.credentials.json` (`claudeAiOauth` access + refresh
  tokens), which *was* on the share. After the first NFS removal the worker
  reported `loggedIn: false` and claude-code-cli requests failed. The file was
  recovered from the retained Longhorn volume (Retain policy left the PV
  intact even after the PVC was deleted), stored as the `claude-cli-credentials`
  Secret, and is now seeded into the worker's writable emptyDir by an init
  container on every pod start. `claude auth status --json` now reports
  `loggedIn: true` (`claude.ai`, pro, refresh token valid to 2026-12-18).
  The init container must remain ordered **after** `containers` in the
  manifest because `k8s/pin-manifest.py` rewrites only the first image line.
  `k8s/claude-code-login.sh` now runs the login in the worker, mirrors the new
  credential into this Secret, and rolls the worker so it takes effect. The
  repo's Codex rotation tools do **not** touch this Secret.

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
- `claude auth status --json` in the worker reports `loggedIn: true`.
- A live `claude-code-cli/sonnet` request returns the real upstream condition
  (`claude_code_cli_quota`) rather than a `TypeError`. The stream parser fix
  (`RemoteCLIAdapter` yields bytes, not str) is covered by
  `tests/test_chat.py::test_remote_cli_adapter_yields_bytes`.
