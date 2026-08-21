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

# Apply manifests (config.yaml carries TUSKER_POOL_* env vars)
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

Tusker Gateway uses the **same `hermes-env-vault` secret** as Hermes. All
provider keys are inherited automatically. Pool definitions are loaded from
`k8s/config.yaml` (mounted as env vars `TUSKER_POOL_CODE`, `TUSKER_POOL_PRIVACY`,
`TUSKER_POOL_PREMIUM`, `TUSKER_POOL_SWARM`).

The gateway reads provider keys for every provider named in those pools:
- `OPENROUTER_API_KEY`
- `MINIMAX_API_KEY` (and similar for `minimax` typo alias, if any)
- `OLLAMA_API_KEY` / `OLLAMA_MAC_API_KEY` (for `ollama-cloud`)
- `OPENCODE_GO_API_KEY`
- `GITHUB_COPILOT_*` (token file paths)
- `CEREBRAS_API_KEY`
- `GOOGLE_API_KEY`
- `SYNTHETIC_API_KEY`

Always verify pool providers exist in `tusker_gateway/config.py:DEFAULT_PROVIDER_REGISTRY`
before adding them — unknown providers raise `ProviderError("Unknown provider: ...")`
which becomes HTTP 502 and exhausts the agent retry budget.

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
| Host | `hermes.tusker.net.au` | `ai.tusker.net.au` |
| PVC | `hermes-home` | `tusker-home` |
| Config | `hermes-env-vault` | `hermes-env-vault` (shared) |

## Migration history

- **2026-08-21** — Migrated the three Codex OAuth credentials (`dmascord@gmail.com`,
  `damien.01@tusker.net.au`, `damien.02@tusker.net.au`) from
  `hermes.tusker.net.au` (deployment `hermes`) to `ai.tusker.net.au` (this
  gateway). The OAuth tokens are stored on the gateway's PVC at
  `/home/tusker/.hermes/auth.json` and rotated by the `CodexTokenRotator`.
  Hermes's pool was cleared to prevent refresh-token reuse collisions.
  `TUSKER_POOL_CODE` and `TUSKER_POOL_PRIVACY` were rebuilt from the live hermes
  pools, plus all `openai-codex/*` models exposed by the migrated catalog.