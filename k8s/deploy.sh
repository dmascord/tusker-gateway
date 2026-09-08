#!/bin/bash
set -euo pipefail

# Tusker AI Gateway — k8s deploy script
# Run on the cluster build host (visor) after the source tree is in place.
#
# Usage:  ./deploy.sh [TAG]
#   TAG  image tag suffix (default: current timestamp)
#
# Source identity must be supplied by the source-sync caller; a build host's
# .git may be stale or absent. TUSKER_COMMIT must be the intended full SHA.
#
# Requires:
#   - source tree at /srv/opencode/tusker-ai-gateway/
#   - buildah installed and configured to push to registry.tusker.net.au:5000
#   - kubectl context pointed at the cluster

NAMESPACE=hermes
DEPLOY=tusker-gateway
REGISTRY=registry.tusker.net.au:5000

# Default to the script's parent directory (the repository root).  An explicit
# SRC_DIR env override is respected so callers can point at a revision-specific
# build directory (e.g. /srv/opencode/tusker-ai-gateway-build-<REV>).
SRC_DIR=${SRC_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

TAG=${1:-$(date +%Y%m%d%H%M%S)}
IMAGE="${REGISTRY}/tusker-gateway:swarm-alpine-${TAG}"

# Never infer identity from remote .git, including on an rsync build host.
if [[ ! "${TUSKER_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: explicitly set TUSKER_COMMIT to the intended full 40-character SHA" >&2
    exit 1
fi
COMMIT=${TUSKER_COMMIT}

echo "=== Deploying Tusker AI Gateway ==="
echo "SRC:     ${SRC_DIR}"
echo "IMAGE:   ${IMAGE}"
echo "COMMIT:  ${COMMIT}"

# --- Build ---
echo "--- Build ---"
cd "${SRC_DIR}"
buildah bud --build-arg "TUSKER_COMMIT=${COMMIT}" -f Dockerfile -t "${IMAGE}" .

# Capture the pushed manifest digest and fail if publication provides none.
WORK_DIR=$(mktemp -d)
trap 'rm -rf "${WORK_DIR}"' EXIT
buildah push --digestfile "${WORK_DIR}/digest" "${IMAGE}"
IMAGE_DIGEST=$(cat "${WORK_DIR}/digest")
if [[ ! "${IMAGE_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "ERROR: push did not provide a valid manifest digest" >&2
    exit 1
fi
echo "Image pushed: ${IMAGE} (digest ${IMAGE_DIGEST})"

# Render the manifest locally without connecting to the cluster (--local).
# Preserves the manifest's environment; the image's baked revision stays
# authoritative. Local rendering never mutates the tracked manifest.
RENDERED="${WORK_DIR}/deployment.json"
kubectl set image -f k8s/deployment.yaml \
    "${DEPLOY}=${IMAGE}" \
    --local -o json > "${RENDERED}"

echo "--- Apply manifests ---"
kubectl -n "${NAMESPACE}" apply -f k8s/pvc-rwx.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/pvc.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/config.yaml
kubectl -n "${NAMESPACE}" apply -f "${RENDERED}"
kubectl -n "${NAMESPACE}" apply -f k8s/service.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/ingressroute.yaml

# --- Rollout ---
echo "--- Rollout ---"
kubectl -n "${NAMESPACE}" rollout status deployment/"${DEPLOY}" --timeout=300s

# Ignore terminating old pods left during rollout, but verify every Ready
# replacement matching the intended image, not an arbitrary items[0] pod.
echo "--- Running image digest verification ---"
kubectl -n "${NAMESPACE}" get pods -l app=tusker-gateway -o json \
    | python3 -c '
import json
import sys

image, digest, container_name = sys.argv[1:]
verified = 0
for pod in json.load(sys.stdin)["items"]:
    if pod["metadata"].get("deletionTimestamp"):
        continue
    status = pod.get("status", {})
    if status.get("phase") != "Running":
        continue
    if not any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in status.get("conditions", [])):
        continue
    containers = pod.get("spec", {}).get("containers", [])
    if not any(c.get("name") == container_name and c.get("image") == image
               for c in containers):
        continue
    current = next((c for c in status.get("containerStatuses", [])
                    if c.get("name") == container_name), None)
    if not current or not current.get("ready") or "running" not in current.get("state", {}):
        raise SystemExit("ERROR: intended pod container is not running and Ready")
    actual = current.get("imageID", "").rsplit("@", 1)[-1]
    if actual != digest:
        raise SystemExit("ERROR: running image digest differs from pushed digest")
    verified += 1
if not verified:
    raise SystemExit("ERROR: no Ready nonterminating pod matches the intended image")
print(f"IMAGE DIGEST OK: {digest} ({verified} Ready pod(s))")
' "${IMAGE}" "${IMAGE_DIGEST}" "${DEPLOY}"

# /health must report the revision baked into the verified image.
echo "--- Commit verification ---"

health_commit=$(curl --fail --retry 24 --retry-delay 5 --retry-all-errors \
    --connect-timeout 5 --max-time 15 -sS \
    "https://ai.tusker.net.au/health" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('commit',''))")

if [ -z "${health_commit}" ]; then
    echo "ERROR: could not read commit from /health"
    exit 1
fi

if [ "${health_commit}" != "${COMMIT}" ]; then
    echo "ERROR: /health commit mismatch — live ${health_commit}, intended ${COMMIT}"
    exit 1
fi
echo "COMMIT OK (/health): ${health_commit}"

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

# --- Chat SSE smoke ---
# Even with maxUnavailable:0, a stale routing table can pin the new pod to
# dead endpoint slices. SSE frames prove the request reaches a live
# worker process.  We use a Python helper that:
#   - parses SSE events in the OpenAI format
#   - checks for non-2xx / errors / incomplete stream / no content
#   - fails closed on any ambiguity
SMOKE_HELPER="${SCRIPT_DIR}/smoke_chat.py"

chat_key=$(kubectl -n "${NAMESPACE}" get secret tusker-env-vault \
  -o jsonpath="{.data.API_KEYS}" | base64 -d | cut -d, -f1)

if [ -z "${chat_key}" ]; then
    echo "ERROR: API_KEYS secret not readable — cannot run chat smoke"
    exit 1
fi

SMOKE_API_KEY="${chat_key}" python3 "${SMOKE_HELPER}" \
    --url "https://ai.tusker.net.au/v1/chat/completions" \
    --model hermes-code


echo "=== Done ==="
