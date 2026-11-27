# AGENTS.md — Tusker AI Gateway

OpenAI-compatible API gateway. Single Python package (`tusker_gateway/`),
aiohttp app, k8s-deployed alongside Hermes.

## Doc index

Read in order when picking up the repo:

| Doc | Purpose |
|---|---|
| `README.md` | Architecture overview (config, auth, pools, routing, app). |
| `docs/solution.md` | Why this exists, scope boundaries. |
| `docs/gateway-model-routing.md` | Pool tiers, heavyweight gate, role aliases, auto_free opt-in, and shadow-first value routing. |
| `docs/deployment-k8s.md` | Cluster topology, source paths, image registry, deploy procedure. |
| `docs/capability-catalog.md` | v0.1.0 capability list (shipped surface). |
| `docs/feature-matrix-and-plan.md` | Roadmap vs. peer gateways. |
| `docs/cleanup-2026-08-19.md` | Earlier cleanup pass — context for current code shape. |
| `docs/migrations/2026-08-21-codex-migration/` | Codex OAuth endpoint move + token-rotation tooling. |
| `docs/zero-downtime-deploys.md` | Why `strategy: Recreate` and the planned RWX migration. |
| `docs/incidents/2026-08-26-usb-ssd-flap.md` | USB-SSD flap postmortem, storage migration, and Longhorn v1.12.1 upgrade notes. |
| `docs/migrations/2026-09-09-config-db/` | DB-backed config store — shipped. Plan, schema, rollout. |
| `docs/incidents/2026-10-21-codex-rotation-and-grok-429-leak.md` | Codex refresh-token rotation + xAI/Grok `{"statusCode":429,...}` envelope leak (req_50a994b481aa7fe3). Operator runbook + hardened cooldown suppression. |
| `docs/incidents/2026-11-01-privacy-pool-codex-seat-collapse.md` | Codex team-seat collapse (`account_id` vs `account_user_id`) + uncorrelated capacity SSE frame (req_ef7626de7c191781). |
| `docs/incidents/2026-11-01-composite-probe-starvation.md` | Composite image-plus-tool evidence never collected: first run scheduled a full interval out, and a bounded probe batch spent every slot on one quota-cooled provider. |
| `docs/incidents/2026-11-08-reasoning-only-length-stop.md` | Reasoning-only `length` turns: dead terminal forwarded to the client + reasoning models below the output floor. |
| `docs/incidents/2026-11-06-longhorn-storage-instability.md` | Longhorn data-path errors degraded the internal registry volume; gateway image pulls failed behind it. Investigation + remediation record. |
| `docs/incidents/2026-11-06-hindsight-memory-startup.md` | Hindsight CrashLoopBackOff from an explicit `HINDSIGHT_API_DATABASE_URL` pointing at a fixed port while embedded `pg0` was still starting. |
| `docs/migrations/2026-11-06-hindsight-postgresql/` | Moving Hindsight from embedded `pg0` to the dedicated `hindsight-postgres` StatefulSet. |
| `docs/incidents/2026-11-11-codex-refresh-cooldown-adoption-deadlock.md` | Codex OAuth refresh-failure cooldown blocked store adoption; the whole pool read as empty while the DB held live credentials. |
| `docs/incidents/2026-11-11-mlx-mac-capacity-queue.md` | MLX Mac capacity rejections discarded servable work (77/80 privacy attempts locally rejected); bounded FIFO admission queue. |
| `docs/incidents/2026-11-16-redaction-token-leak.md` | Opaque `[TUSKER:email:…]` ciphertext reaching OMP sessions; syntax-shaped redaction aliases enabled in production. |
| `docs/incidents/2026-11-20-xhigh-reasoning-effort-downgrade.md` | `xhigh` reasoning level rejected by non-Codex upstreams; per-provider downgrade to `high` + manifest re-pin to the running digest. |
| `docs/incidents/2026-11-22-failover-terminal-error.md` | Exhausted pool failover reported only the last candidate's timeout; aggregate all candidate failures in the terminal error. |
| `docs/incidents/2026-11-22-privacy-probe-starvation.md` | Privacy probe re-tested the same quota-blocked prefix forever; interleave due routes across providers. Includes context-window evidence check. |
| `docs/incidents/2026-11-22-synthetic-image-tools-qualification.md` | Four gates for synthetic image+tools routing; composite probe output cap and 120s timeout. |
| `docs/incidents/2026-11-23-privacy-pool-full-audit.md` | All 200 privacy routes audited against four gates; 8 eligible for image+tools+212K. |
| `docs/incidents/2026-11-23-code-pool-audit.md` | 355 code routes audited; 24 eligible for image+tools+212K, google modality probes added three. |
| `docs/incidents/2026-11-23-worker-pod-verification.md` | opencode worker verified end-to-end; kilo worker stale/unused, kilo models work over direct HTTP. |
| `docs/incidents/2026-11-23-kilo-qualification.md` | 15/17 kilo models in code pool; 10 tool-qualified, 5 genuine upstream failures; 2 audio models correctly excluded. |
| `docs/audio-routing.md` | OpenAI-compatible audio endpoints, provider selection, chat audio input and capability evidence. |
| `docs/context-efficiency.md` | Context-pressure advisory headers, Anthropic document-block rejection, RTK compression honesty and the evidence behind each decision. |
| `docs/provider-health.md` | `GET /v1/provider-health` — router-level pool health for clients, the OMP extension that renders it, and the usage-row contract notes. |
| `docs/local-cli-context-notes.md` | What OMP and OpenCode already do for context/token efficiency, and the local config gaps the gateway cannot fill. |
| `docs/resilience-performance-assessment.md` | Measured resilience/performance gaps: Postgres backup and retention, unscheduled prunes, safe gateway HA, and what is already fixed. Includes the live verification of the resilience release. |
| `docs/audit-database.md` | PostgreSQL-backed integrity-chained audit log: schema, locking, retention, migration and verification. |
| `docs/nfs-retirement.md` | RWX/NFS volume retired: pod-local emptyDir, historical audit archive, verification. |
| `docs/incidents/2026-11-23-audio-routing.md` | Restored accidentally-removed TTS route; added STT and audio token accounting; verified live. |
| `docs/incidents/2026-11-23-all-rate-limited-failover.md` | Uniform rate-limit exhaustion now returns 429 with Retry-After instead of a generic 502. |
| `docs/incidents/2026-11-23-claude-code-cli-sse-escape.md` | Worker error frames used literal backslash-n; claude-code-cli failures surfaced as a TypeError. |
| `docs/incidents/2026-11-24-audit-flock-eio.md` | NFS flock EIO on the shared audit log failed requests closed and mislabelled them as upstream errors; share-manager restart + accurate message + sync retry backoff. |
| `docs/incidents/2026-11-24-composite-probe-fixture.md` | Composite image+tools probe required a middle-stripe colour the fixture never contained (2x2 quadrants), so honest vision models scored `unsupported`; striped fixture + `image-tool-contract-v3`. |
| `docs/incidents/2026-11-25-postgres-configmap-audit-503.md` | Missing `tusker-gateway-postgres-config` ConfigMap kept Postgres in `Init:0/1`; fail-closed audit writes returned 503 `audit_unavailable`; deploy.sh now applies the ConfigMap. |
| `docs/incidents/2026-11-25-direct-route-cooldown-bypass.md` | Explicit `provider/model` routes bypassed pool selection and therefore the cooldown filter, so quarantined routes were still dispatched upstream; composite probe runner had no quarantine filter at all. |
| `docs/incidents/2026-11-25-gateway-followup-audit.md` | Post-resilience audit: logical backup missing from the deploy path, truncated dumps published as success, Anthropic cooldown bypass, reload-rebuilt rotators losing the refresh lock, provider-health missing capacity quarantines, retention gated behind unrelated switches, committed Grafana password. |
| `docs/incidents/2026-11-27-mlx-mac-route-sizing.md` | MLX Mac route sizing: context admission under-count (18-28% on code/JSON), the `developer`-role 500 on Ollama, Qwen window right-sized to 128k, and Inkling 2 Mini's self-closing tool-call markup. |
| `docs/incidents/2026-11-27-auto-discovered-model-worker-error.md` | Auto-discovered `opencode-cli` model advertised by `opencode models` but rejected at inference; worker proxy flattened 410/401 into `worker_error` so the dead route was never pruned (req_6cf0e5a0d1d0bbd7). |
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

### Buildah fallback and retained cache

The normal gateway build host remains `visor`; its SSH path is documented in `docs/deployment-k8s.md`. If `visor` is unavailable, builds MAY run from the retained privileged Buildah Deployment in the `hermes` namespace. Its manifest is `k8s/buildah-cache.yaml`.

The cache workload is `tusker-gateway-buildah`, using image `quay.io/buildah/stable@sha256:4f47edc1eace7a3fcbafb2c4bb784d13225986c1253e321f68c7229a33d628a0`, PVC `tusker-gateway-buildah-cache` (50 GiB, Longhorn), and a Recreate strategy because the PVC is ReadWriteOnce. The PVC is mounted at `/var/lib/containers` and `/workspace`; Buildah's graphroot is therefore retained across pod restarts and future builds.

The fallback MUST use an exact `git archive` of the intended committed source, an explicit full `TUSKER_COMMIT`, `buildah bud --layers`, and a digest-pinned deployment. Namespace container ephemeral storage is capped at 4 GiB, so Buildah storage MUST stay on the PVC. Delete only revision-specific temporary source/PVC resources; do not delete the retained cache PVC after a normal build.

The registry remains the durable artifact store. The retained PVC is a performance cache, not the source of truth: it MAY be cleared and rebuilt from the registry/source archive. Do not place registry credentials, API keys, or source archives containing secrets in this repository. Verify the pushed digest, running pod imageID, embedded `/health` commit, and live route before cleanup.

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
