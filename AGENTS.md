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
~358 passed + 2 skipped.

## Memory

Durable user preferences and project decisions live in the harness memory
bank. Recall before answering questions about prior choices; retain when
making new ones that should persist across sessions.
