# Kubernetes deployment: Tusker Gateway

Tusker Gateway runs alongside Hermes in the `hermes` namespace on the `visor` cluster.

## Source paths

| Where | Path |
|---|---|
| Source on visor | `/srv/opencode/tusker-gateway/` (not yet set up — see below) |
| Image registry | `registry.tusker.net.au:5000/tusker-gateway` |
| Manifests | `tusker-gateway/k8s/` |

## 1. Push source to visor

Tusker Gateway is a standalone repo, not yet tracked in git. Push it to `dmascord/tusker-gateway` before the first build.

## 2. Build and deploy

From Mac:
```bash
ssh visor
```

On visor:
```bash
cd /srv/opencode/tusker-gateway

# Build
TAG=swarm-alpine-$(date +%Y%m%d%H%M%S)
IMAGE=registry.tusker.net.au:5000/tusker-gateway:$TAG
buildah bud -f Dockerfile -t "$IMAGE" .
buildah push "$IMAGE"

# Apply manifests
kubectl -n hermes apply -f k8s/pvc.yaml
kubectl -n hermes apply -f k8s/service.yaml
kubectl -n hermes apply -f k8s/ingressroute.yaml

# Deploy
kubectl -n hermes set image deployment/tusker-gateway tusker-gateway="$IMAGE"
kubectl -n hermes rollout status deployment/tusker-gateway --timeout=120s
```

## 3. Smoke test

```bash
# In-cluster
kubectl -n hermes get pods -o wide | grep tusker-gateway
curl -sS https://ai.tusker.net.au/health && echo
curl -sS https://ai.tusker.net.au/ready && echo

# Public edge
curl -sS -o /dev/null -w 'http=%{http_code} time=%{time_total}s\n' https://ai.tusker.net.au/health
curl -sS https://ai.tusker.net.au/ready && echo
```

## 4. DNS

Point `ai.tusker.net.au` → `103.68.121.242` (public LB IP).

## 5. Configuration

Tusker Gateway uses the **same `hermes-env-vault` secret** as Hermes. All provider keys, pool definitions, and model fallbacks are inherited automatically.

The gateway reads:
- `OPENROUTER_API_KEY` — live key from vault
- `HERMES_CODE_FALLBACK_*` / `HERMES_PRIVACY_FALLBACK_*` — pool definitions
- All other provider keys (GitHub Copilot, Gemini, Groq, etc.)

No separate secret is required.

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