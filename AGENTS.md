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
| `docs/incidents/2026-10-21-codex-rotation-and-grok-429-leak.md` | Codex refresh-token rotation + xAI/Grok `{"statusCode":429,...}` envelope leak (req_50a994b481aa7fe3). Operator runbook + hardened cooldown suppression. |
| `TODO.md` | Active cross-session work items. |


Topic-specific:

| Doc | Topic |
|---|---|
| `docs/audit-2026-09-25-gateway-improvements.md` | Comprehensive security, API, reliability, and ops audit with implementation plan. |
| `IMAGE_VIDEO_GENERATION_ANALYSIS.md` | Provider key audit (2026-08-24), endpoint shape vs. configured URLs. |
| `OTHER_PROVIDERS_CAPABILITIES.md` | Per-provider capability survey beyond chat. |
| `README_IMAGE_GENERATION.md` | Image gen architecture (OpenAI GPT Image + Codex pathway). |
| `IMAGE_GENERATION_IMPLEMENTATION_PLAN.md` | Phased plan for image/video wiring. |
| `IMPLEMENTATION_SUMMARY.md` / `IMPLEMENTATION_COMPLETE.md` | Milestones + acceptance evidence. |
| `docs/qwen3.8-27b-perf-2026-09-23.md` | Qwen3.8-27B local and gateway benchmark, including vision verification. |
| `docs/turn-capture-replay.md` | DB-backed rejected-turn capture: retention, privacy policy, admin/replay ops. |

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
~1656 passed + 8 skipped (offline, 2026-10-16; use `--ignore=tests/test_passthrough_providers.py`).

`tests/test_fault_scenarios.py` exercises historical stream/provider fault
classes with mock upstream injection: mid-stream close, transient 502,
request-level 413, repeated-reasoning cycles, and a real OMP-shaped
`hermes-code` turn from `tests/fixtures/hermes_code_turn.json`. These run
in the offline suite and protect against regression in the recovery
semantics (failover bounds, cooldown classification, cycle capture
write) that production traffic depended on through 2026-10.

### Mid-stream upstream close: vLLM/OpenRouter-shape error frame

When an upstream SSE stream closes mid-flight (peer reset, no
`[DONE]`, no `finish_reason`) the gateway cannot fall over after the
HTTP 200 has been committed. `diagnostic_raw_stream` re-raises the
failure as `ProviderStreamClosedError`, which reaches the same
post-commit error path as `ProviderStreamLoopError` /
`ProviderEmptyStreamError` and emits a vLLM/OpenRouter-shape
`data: {"error": {...}}` frame followed by `data: [DONE]`. The OpenAI
SDK raises `APIError` on receiving the top-level `error` object
instead of absorbing the failure into a successful assistant turn;
the OMP path no longer waits on its 300s read timeout to verdict
(2026-10-14 00:17Z case). A bare `ConnectionResetError` from the
*client* write side still hits the silent
`stream client disconnected mid-flight` teardown — the two are
distinguished by exception type, not by request flag.

## Secure production smoke testing

Run an authenticated production smoke without storing or printing a gateway
credential:

```bash
k8s/smoke_production.sh
```

The wrapper connects to `tusker@tusker.duckdns.org` on SSH port `8022`, reads
the first key from `hermes/tusker-env-vault`'s `API_KEYS` field on `visor`, and
passes it only to the existing smoke helper in the remote shell. The key is
never written to `.env`, emitted in output, or placed on the command line.
The default request uses `hermes-privacy`; use `--model hermes-code` for the
general pool or `--with-media` to include embedding/rerank checks.

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

## Runtime configuration (DB-backed config store)

When `TUSKER_CONFIG_DATABASE_ENABLED=1` (production), the **database is the
source of truth** for pool and provider configs. `TUSKER_POOL_*` environment
variables in `k8s/deployment.yaml` are only a fallback if the database is
unreachable.

To change runtime configuration:

1. Edit `k8s/deployment.yaml` (the yaml remains the durable source of truth)
2. Sync into the DB: `python -m tusker_gateway.tools.migrate_config_to_db --update`
   (run on visor against the state DB with the usual env vars set)
3. Config hot-reloads (generation N → N+1), no pod restart needed

**Never edit deployment.yaml alone and expect it to change live behavior** —
the DB retains its previous definition and shadows the env vars. Use the
migration tool (or the admin API `PUT /admin/pools/{pool}` / `PUT
/admin/providers/{provider}`) to propagate changes.

## Client-profile normalization (Phase 0 shadow release)

`tusker_gateway/normalization.py` resolves a `ClientProfile` per request
(see `docs/investigation-2026-10-16-ua-normalization.md` for the locked
precedence rules and profile set). Phase 0 is **log + span only** — no
client-visible bytes change. The resolver runs in shadow mode by
default, gated on `TUSKER_NORMALIZATION_SHADOW` (per-request env
read, no startup caching; default `1` so the very first deploy of Phase
0 ships in shadow mode without operator action). Set to `0` to disable
the resolver and stop emitting `tusker.client_profile` access-log and
span attributes. Phase 1 (the error-envelope dispatcher) is the only
consumer of `tusker_shadow_shape_mismatch_total{profile, surface}`;
that counter stays at `0` until Phase 1 ships.
