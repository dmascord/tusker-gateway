#!/bin/bash
set -euo pipefail

# Tusker Gateway K8s deploy script
# Run on visor (10.0.0.231) after building image

NAMESPACE=hermes
DEPLOY=tusker-gateway
REGISTRY=registry.tusker.net.au:5000
TAG=${1:-$(date +%Y%m%d%H%M%S)}
IMAGE="${REGISTRY}/tusker-gateway:swarm-alpine-${TAG}"

echo "=== Deploying Tusker Gateway ==="
echo "IMAGE: ${IMAGE}"

# Build image
echo "--- Build ---"
cd /Volumes/dev/dev/hermes/tusker-gateway
buildah bud -f Dockerfile -t "${IMAGE}" .
buildah push "${IMAGE}"
buildah images --format '{{.ID}} {{.Name}}:{{.Tag}}' | grep -q "${IMAGE}" || { echo "ERROR: image push failed"; exit 1; }
echo "Image pushed: ${IMAGE}"

# Apply manifests
echo "--- Apply manifests ---"
kubectl -n "${NAMESPACE}" apply -f k8s/pvc.yaml -f k8s/config.yaml -f k8s/service.yaml -f k8s/ingressroute.yaml

# Update deployment image
echo "--- Rollout ---"
kubectl -n "${NAMESPACE}" set image deployment/"${DEPLOY}" "${DEPLOY}=${IMAGE}"
kubectl -n "${NAMESPACE}" rollout status deployment/"${DEPLOY}" --timeout=120s

# Smoke tests
echo "--- Smoke test ---"
kubectl -n "${NAMESPACE}" get pods -o wide | grep -E 'tusker-gateway|NAME'
curl -sS -o /dev/null -w "http=%{http_code} time=%{time_total}s\n" "https://ai.tusker.net.au/health"
curl -sS -o /dev/null -w "http=%{http_code} time=%{time_total}s\n" "https://ai.tusker.net.au/ready"

echo "=== Done ==="
