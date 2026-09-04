# Kubernetes deployment: Tusker Gateway

Tusker Gateway runs alongside Hermes in the `hermes` namespace on the `visor` cluster.

## Source paths

| Where | Path |
|---|---|
| Local checkout | `~/dev/tusker-ai-gateway/` |
| Source on visor | `/srv/opencode/tusker-ai-gateway/` |
| Image registry | `registry.tusker.net.au:5000/tusker-gateway` |
| Manifests | `tusker-ai-gateway/k8s/` |

## 1. Sync source to visor

The build host is `visor`. The repo lives at `~/dev/tusker-ai-gateway/` locally and must be mirrored to `/srv/opencode/tusker-ai-gateway/` on `visor` before building.

```bash
# from Mac
rsync -a --delete --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
  ~/dev/tusker-ai-gateway/ visor:/srv/opencode/tusker-ai-gateway/
```

## 2. Build and deploy

The convenience script `k8s/deploy.sh` does all of this in one shot:

```bash
ssh visor
cd /srv/opencode/tusker-ai-gateway
./k8s/deploy.sh                # optional: ./k8s/deploy.sh 20260820180000
```

Manual equivalent:

```bash
ssh visor
cd /srv/opencode/tusker-ai-gateway

# Build
TAG=swarm-alpine-$(date +%Y%m%d%H%M%S)
IMAGE=registry.tusker.net.au:5000/tusker-gateway:$TAG
buildah bud -f Dockerfile -t "$IMAGE" .
buildah push "$IMAGE"

# Apply manifests. The current pool JSON is inline in deployment.yaml;
# config.yaml currently only ensures the namespace exists.
kubectl -n hermes apply -f k8s/pvc.yaml
kubectl -n hermes apply -f k8s/config.yaml
kubectl -n hermes apply -f k8s/service.yaml
kubectl -n hermes apply -f k8s/ingressroute.yaml

# Deploy
kubectl -n hermes set image deployment/tusker-gateway tusker-gateway="$IMAGE"
kubectl -n hermes rollout status deployment/tusker-gateway --timeout=180s
```

## 3. Smoke test

The gateway is fronted by `ai.tusker.net.au` (same edge as Hermes):

```bash
# Pod health
kubectl -n hermes get pods -o wide | grep tusker-gateway

# HTTP health
curl -sS -o /dev/null -w 'health http=%{http_code} time=%{time_total}s\n' \
  https://ai.tusker.net.au/health
curl -sS -o /dev/null -w 'ready  http=%{http_code} time=%{time_total}s\n' \
  https://ai.tusker.net.au/ready
curl -sS https://ai.tusker.net.au/ready && echo
```

## 4. DNS

`ai.tusker.net.au` already points at the cluster LB. The gateway shares the
edge with Hermes — no DNS work needed for it.

## 5. Configuration

Tusker Gateway uses the isolated `tusker-env-vault` secret. The deployment
imports its provider keys with `envFrom` and declares the pool JSON inline in
`k8s/deployment.yaml` as `TUSKER_POOL_CODE`, `TUSKER_POOL_PRIVACY`,
`TUSKER_POOL_PREMIUM`, and `TUSKER_POOL_SWARM`.

OAuth credentials are separate pools: Codex uses `CODEX_CREDENTIALS` (or the
provider-specific `OPENCODE_CODEX_CREDENTIALS`), public Copilot uses
`GITHUB_COPILOT_CREDENTIALS`, and Enterprise Copilot uses
`GITHUB_COPILOT_ENTERPRISE_CREDENTIALS`. Keep these keys in
`tusker-env-vault`; a bearer API key alone does not populate an OAuth pool.

The gateway reads provider keys for every provider named in those pools:
- `OPENROUTER_API_KEY`
- `MINIMAX_API_KEY` (and similar for `minimax` typo alias, if any)
- `OLLAMA_API_KEY` / `OLLAMA_MAC_API_KEY` (for `ollama-cloud`)
- `OPENCODE_GO_API_KEY`
- `GROQ_API_KEY`
- `ARCEEAI_API_KEY`
- `GITHUB_COPILOT_*` (OAuth credential pools)
- `CEREBRAS_API_KEY`
- `GEMINI_API_KEY`
- `COHERE_API_KEY`
- `ZAI_API_KEY`
- `XIAOMI_API_KEY`
- `NVIDIA_API_KEY` (retained in the isolated secret, but direct NVIDIA
  catalog discovery is disabled while its upstream capacity is saturated)
- `SYNTHETIC_API_KEY`

Always verify pool providers exist in `tusker_gateway/config.py:DEFAULT_PROVIDER_REGISTRY`
before adding them — unknown providers raise `ProviderError("Unknown provider: ...")`
which becomes HTTP 502 and exhausts the agent retry budget.

When checking OMP routing, use the standalone provider configured as
`tusker-gateway` with `https://ai.tusker.net.au/v1`. The legacy
`hermes-gateway` provider's `/v1` requests continue to use the old hostname
for compatibility, but terminate at `tusker-gateway`; cross-host redirects
would cause some clients to drop `Authorization` on API POSTs. All non-API
Hermes traffic is redirected to the AI hostname. The legacy Hermes deployment
is scaled to zero after cutover; its PVC, service, and secrets remain intact as
a rollback target. Changes to this deployment's `TUSKER_POOL_CODE` therefore
apply to both API hostnames during the compatibility transition.

## 6. Rollback

```bash
kubectl -n hermes rollout undo deployment/tusker-gateway
```

## 7. Teardown

```bash
kubectl -n hermes delete ingressroute tusker-gateway
kubectl -n hermes delete service tusker-gateway
kubectl -n hermes delete deployment tusker-gateway
kubectl -n hermes delete pvc tusker-home
```

## Key differences from Hermes

| | Hermes | Tusker Gateway |
|---|---|---|
| Name | `hermes` | `tusker-gateway` |
| Image | `hermes-agent` | `tusker-gateway` |
| Host | `hermes.tusker.net.au` (API compatibility + redirect) | `ai.tusker.net.au` |
| PVC | `hermes-home` | `tusker-home` |
| Config | `hermes-env-vault` | `tusker-env-vault` (isolated) |

## Migration history

- **2026-08-21** — Migrated the three Codex OAuth credentials (`dmascord@gmail.com`,
  `damien.01@tusker.net.au`, `damien.02@tusker.net.au`) from
  `hermes.tusker.net.au` (deployment `hermes`) to `ai.tusker.net.au` (this
  gateway). The OAuth tokens are stored on the gateway's PVC at
  `/home/tusker/.hermes/auth.json` and rotated by the `CodexTokenRotator`.
  Hermes's pool was cleared to prevent refresh-token reuse collisions.
  `TUSKER_POOL_CODE` and `TUSKER_POOL_PRIVACY` were rebuilt from the live hermes
  pools, plus all `openai-codex/*` models exposed by the migrated catalog.
- **2026-09-04** — Cut the `hermes.tusker.net.au` `/v1/` API route over to
  `tusker-gateway` without changing DNS, OMP configuration, or the Hermes web
  routes. Merged the three legacy Hermes client-key identities into
  `tusker-env-vault` (seven unique gateway-accepted keys total), changed the
  deployment to consume `API_KEYS` from that secret, and added compatibility
  entries for the legacy model catalog.
- **2026-09-04** — Redirected non-API `hermes.tusker.net.au` traffic to
  `ai.tusker.net.au` on both Traefik entrypoints, retained a transparent
  `/v1` compatibility route to `tusker-gateway` so authenticated API POSTs do
  not lose their credentials across a host redirect, and scaled the legacy
  Hermes deployment to zero. The legacy PVC, service, and secrets remain
  available for rollback.
