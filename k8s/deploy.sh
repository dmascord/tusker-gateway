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
SRC_DIR=/srv/opencode/tusker-ai-gateway
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
curl -sS -o /dev/null -w "health http=%{http_code} time=%{time_total}s\n" "https://ai.tusker.net.au/health"
curl -sS -o /dev/null -w "ready  http=%{http_code} time=%{time_total}s\n" "https://ai.tusker.net.au/ready"
curl -sS "https://ai.tusker.net.au/ready" && echo

echo "=== Done ==="