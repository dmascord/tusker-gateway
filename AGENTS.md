# AGENTS.md — Tusker AI Gateway

OpenAI-compatible API gateway. Single Python package (`tusker_gateway/`),
aiohttp app, k8s-deployed alongside Hermes.

## Doc index

Read in order when picking up the repo:

| Doc | Purpose |
|---|---|
| `README.md` | Architecture overview (config, auth, pools, routing, app). |
| `docs/solution.md` | Why this exists, scope boundaries. |
| `docs/gateway-model-routing.md` | Pool tiers, heavyweight gate, role aliases, auto_free opt-in. |
| `docs/deployment-k8s.md` | Cluster topology, source paths, image registry, deploy procedure. |
| `docs/capability-catalog.md` | v0.1.0 capability list (shipped surface). |
| `docs/feature-matrix-and-plan.md` | Roadmap vs. peer gateways. |
| `docs/cleanup-2026-08-19.md` | Earlier cleanup pass — context for current code shape. |
| `docs/migrations/2026-08-21-codex-migration/` | Codex OAuth endpoint move + token-rotation tooling. |
| `docs/zero-downtime-deploys.md` | Why `strategy: Recreate` and the planned RWX migration. |
| `docs/incidents/2026-08-26-usb-ssd-flap.md` | USB-SSD flap postmortem, storage migration, and Longhorn v1.12.1 upgrade notes. |
| `docs/migrations/2026-09-09-config-db/` | DB-backed config store — shipped. Plan, schema, rollout. |
| `TODO.md` | Active cross-session work items. |


Topic-specific:

| Doc | Topic |
|---|---|
| `IMAGE_VIDEO_GENERATION_ANALYSIS.md` | Provider key audit (2026-08-24), endpoint shape vs. configured URLs. |
| `OTHER_PROVIDERS_CAPABILITIES.md` | Per-provider capability survey beyond chat. |
| `README_IMAGE_GENERATION.md` | Image gen architecture (OpenAI GPT Image + Codex pathway). |
| `IMAGE_GENERATION_IMPLEMENTATION_PLAN.md` | Phased plan for image/video wiring. |
| `IMPLEMENTATION_SUMMARY.md` / `IMPLEMENTATION_COMPLETE.md` | Milestones + acceptance evidence. |

Source of truth for runtime config: `tusker_gateway/config.py`
(`DEFAULT_PROVIDER_REGISTRY`, `PoolConfig`). Manifest source: `k8s/`.

## Destructive-action policy

Confirm before any of: `kubectl rollout undo | delete deployment | delete
pod`, `kubectl apply` on `k8s/deployment.yaml` / `k8s/config.yaml`,
`git push --force` to `main`, `git reset --hard` past HEAD, `rm -rf` on
the source dirs, anything touching the `hermes-env-vault` secret.

Read-only ops (`kubectl get|logs|describe|rollout status`, `git status`) are
always fine.

## Deploy flow

`rsync` source to `visor:/srv/opencode/tusker-ai-gateway/`, then run
`./k8s/deploy.sh` on visor (buildah build → push → apply manifests → rollout
→ smoke-test `/health` and `/ready`). See `docs/deployment-k8s.md`.

## Testing

`pytest tests/ -p no:cacheprovider` — skip
`tests/test_passthrough_providers.py` for offline runs (hits live upstreams).
~894 passed + 3 skipped.

## visor USB-flap monitor — DEPLOYMENT STATUS UNCLEAR (2026-09-14 audit)

`tusker_gateway/tools/usb-flap-monitor.{sh,service,timer}` exists in the
repo. As of 2026-09-14, the systemd timer is **not installed on visor**
(`systemctl list-unit-files | grep usb-flap` returns nothing, the script
is missing from `/usr/local/bin/`). The previous deployment is no
longer running.

Whether to re-deploy depends on operational policy — the T5 is still
physically attached to visor (`/dev/sdc`, 931 GB) and
`/mnt/longhorn-ssd` is still a Longhorn disk with `allowScheduling: false,
evictionRequested: true`. No live replicas currently sit on the T5, so
the monitor would only catch disturbances that don't take down a
service. If the goal is "smoke detector" only, re-deployment is
appropriate; if the drive is being decommissioned, removal from docs
is appropriate.

The previous incident doc says: "When the drive is physically unplugged
(follow-up in `docs/incidents/2026-08-26-usb-ssd-flap.md`), the timer +
script can be removed (`systemctl disable --now usb-flap-monitor.timer`)."
See `TODO.md` for the active decision.

## Memory

Durable user preferences and project decisions live in the harness memory
bank. Recall before answering questions about prior choices; retain when
making new ones that should persist across sessions.
