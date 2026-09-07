#!/bin/bash
set -euo pipefail

# Tusker AI Gateway — k8s deploy script
# Run on the cluster build host (visor) after the source tree is in place.
#
# Usage:  ./deploy.sh [TAG]
#   TAG  image tag suffix (default: current timestamp)
#
# Requires:
#   - source tree at /srv/opencode/tusker-ai-gateway/
#   - buildah installed and configured to push to registry.tusker.net.au:5000
#   - kubectl context pointed at the cluster

NAMESPACE=hermes
DEPLOY=tusker-gateway
REGISTRY=registry.tusker.net.au:5000
# Use the provided SRC_DIR if the default doesn't exist, otherwise default
if [ -d "/srv/opencode/tusker-ai-gateway" ]; then
    SRC_DIR=/srv/opencode/tusker-ai-gateway
else
    # Fallback to current directory (useful for local testing)
    SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fi
TAG=${1:-$(date +%Y%m%d%H%M%S)}
IMAGE="${REGISTRY}/tusker-gateway:swarm-alpine-${TAG}"

echo "=== Deploying Tusker AI Gateway ==="
echo "SRC:   ${SRC_DIR}"
echo "IMAGE: ${IMAGE}"

# --- Build ---
echo "--- Build ---"
cd "${SRC_DIR}"
COMMIT=${TUSKER_COMMIT:-$(git rev-parse --short HEAD 2>/dev/null || echo unknown)}
echo "COMMIT: ${COMMIT}"
buildah bud --build-arg "TUSKER_COMMIT=${COMMIT}" -f Dockerfile -t "${IMAGE}" .
buildah push "${IMAGE}"
buildah images --format '{{.ID}} {{.Name}}:{{.Tag}}' | grep -q "${IMAGE}" \
  || { echo "ERROR: image push failed"; exit 1; }
echo "Image pushed: ${IMAGE}"

# --- Apply manifests ---
echo "--- Apply manifests ---"
kubectl -n "${NAMESPACE}" apply -f k8s/pvc-rwx.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/pvc.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/config.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/deployment.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/service.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/ingressroute.yaml

# --- Rollout ---
echo "--- Rollout ---"
kubectl -n "${NAMESPACE}" set image deployment/"${DEPLOY}" "${DEPLOY}=${IMAGE}"
kubectl -n "${NAMESPACE}" rollout status deployment/"${DEPLOY}" --timeout=180s

# --- Smoke test ---
echo "--- Smoke test ---"
kubectl -n "${NAMESPACE}" get pods -o wide | grep -E 'tusker-gateway|NAME'

smoke_check() {
  local label="$1"
  local path="$2"
  # RollingUpdate with maxUnavailable:0 keeps the old pod serving until the
  # new pod is Ready, but ingress EndpointSlice propagation can still lag
  # briefly behind the pod condition. Retry transient 503/no-endpoint
  # responses instead of reporting a false failure.
  curl --fail --retry 24 --retry-delay 5 --retry-all-errors \
    --connect-timeout 5 --max-time 15 -sS \
    -o /dev/null -w "${label} http=%{http_code} time=%{time_total}s\\n" \
    "https://ai.tusker.net.au${path}"
}

smoke_check health /health
smoke_check ready /ready
curl --retry 24 --retry-delay 5 --retry-all-errors \
  --connect-timeout 5 --max-time 15 -sS "https://ai.tusker.net.au/ready" && echo

# Exercise /v1/chat/completions with stream:true through the public ingress.
# Even with maxUnavailable:0, a stale routing table can pin the new pod to
# dead endpoint slices — SSE frames prove the request reaches a live
# worker process.
chat_payload='{"model":"hermes-code","stream":true,"messages":[{"role":"user","content":"Reply with exactly the word DONE."}]}'
chat_key=$(kubectl -n "${NAMESPACE}" get secret tusker-env-vault \
  -o jsonpath="{.data.API_KEYS}" | base64 -d | cut -d, -f1 || true)
if [ -n "${chat_key}" ]; then
  curl -sS -N --connect-timeout 5 --max-time 20 \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${chat_key}" \
    -X POST "https://ai.tusker.net.au/v1/chat/completions" \
    -d "${chat_payload}" | head -c 4096 || true
  echo
else
  echo "API_KEYS secret not readable — skipping chat smoke test"
fi

echo "=== Done ==="
