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
# Env:  FORCE_TAG=1 replaces an existing image tag (default: refuse, because a
#       tag must name exactly one binary).
#       TUSKER_PIN_MANIFEST=1 writes the built digest into k8s/deployment.yaml
#       (default: print the pin so the workstation can commit it).
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

# Registry provenance helpers, shared with k8s/verify-provenance.sh.
PROV_REGISTRY="${REGISTRY}"
# shellcheck source=k8s/lib-provenance.sh
source "${SCRIPT_DIR}/lib-provenance.sh"

TAG=${1:-$(date +%Y%m%d%H%M%S)}
IMAGE_TAG="swarm-alpine-${TAG}"
IMAGE="${REGISTRY}/tusker-gateway:${IMAGE_TAG}"

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

# A tag must name exactly one binary: refuse to overwrite a published tag. The
# deploy pins the digest it built, so replacing a tag cannot change what the
# running spec executes - but a moving tag stops being a usable reference.
if existing_digest=$(prov_registry_digest "${IMAGE_TAG}"); then
    if [[ "${FORCE_TAG:-0}" != "1" ]]; then
        echo "ERROR: ${IMAGE} already exists (digest ${existing_digest})" >&2
        echo "       Re-run with FORCE_TAG=1 to replace it." >&2
        exit 1
    fi
    echo "WARNING: replacing existing tag ${IMAGE_TAG} (was ${existing_digest})"
fi

# --- Build ---
echo "--- Build ---"
cd "${SRC_DIR}"
buildah bud --layers --build-arg "TUSKER_COMMIT=${COMMIT}" -f Dockerfile -t "${IMAGE}" .

# Capture the pushed manifest digest and fail if publication provides none.
WORK_DIR=$(mktemp -d)
trap 'rm -rf "${WORK_DIR}"' EXIT
buildah push --digestfile "${WORK_DIR}/digest" "${IMAGE}"
IMAGE_DIGEST=$(cat "${WORK_DIR}/digest")
if [[ ! "${IMAGE_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "ERROR: push did not provide a valid manifest digest" >&2
    exit 1
fi
IMAGE_REF="${REGISTRY}/tusker-gateway@${IMAGE_DIGEST}"
echo "Image pushed: ${IMAGE} -> ${IMAGE_REF}"

# Render the manifest locally without connecting to the cluster (--local).
# The spec is pinned to the immutable digest, so a re-push of a tag can never
# change what a restarted pod runs. Local rendering never mutates the tracked
# manifest; k8s/pin-manifest.py prints the pin for the operator to commit.
RENDERED="${WORK_DIR}/deployment.json"
kubectl set image -f k8s/deployment.yaml \
    "${DEPLOY}=${IMAGE_REF}" \
    --local -o json > "${RENDERED}"

# The Kilo CLI runs in a separate, resource-limited pod on wynk. Render the
# exact same immutable-by-tag build into its manifest before applying it.
KILO_WORKER_RENDERED="${WORK_DIR}/kilo-worker.yaml"
sed "s|${REGISTRY}/tusker-gateway:latest|${IMAGE_REF}|g" \
    k8s/kilo-worker.yaml > "${KILO_WORKER_RENDERED}"

echo "--- Apply manifests ---"
kubectl -n "${NAMESPACE}" apply -f "${KILO_WORKER_RENDERED}"
kubectl -n "${NAMESPACE}" rollout status deployment/tusker-kilo-worker --timeout=300s
kubectl -n "${NAMESPACE}" apply -f k8s/pvc-rwx.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/pvc.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/config.yaml
kubectl -n "${NAMESPACE}" apply -f "${RENDERED}"
kubectl -n "${NAMESPACE}" apply -f k8s/service.yaml
kubectl -n "${NAMESPACE}" apply -f k8s/ingressroute.yaml

# --- Rollout ---
echo "--- Rollout ---"
kubectl -n "${NAMESPACE}" rollout status deployment/"${DEPLOY}" --timeout=300s

# Record the build identity on the live objects so a drift check can compare them
# against git without guessing.
for target in "${DEPLOY}" tusker-kilo-worker; do
    kubectl -n "${NAMESPACE}" annotate deployment/"${target}" \
        "tusker.net.au/commit=${COMMIT}" \
        "tusker.net.au/image-tag=${IMAGE_TAG}" \
        "tusker.net.au/image-digest=${IMAGE_DIGEST}" \
        --overwrite > /dev/null
done

# Ignore terminating old pods left during rollout, but verify every Ready
# replacement matching the intended image, not an arbitrary items[0] pod. The
# same check runs for the kilo worker, which runs the same image.
verify_image_digest() {
    local selector=$1 container=$2
    kubectl -n "${NAMESPACE}" get pods -l "${selector}" -o json \
        | python3 -c '
import json
import sys

digest, image_ref, container_name = sys.argv[1:]
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
    if not any(c.get("name") == container_name and c.get("image") == image_ref
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
print("{}: IMAGE DIGEST OK ({} Ready pod(s), {})".format(container_name, verified, digest))
' "${IMAGE_DIGEST}" "${IMAGE_REF}" "${container}"
}

echo "--- Running image digest verification ---"
verify_image_digest app=tusker-gateway "${DEPLOY}"
verify_image_digest app=tusker-kilo-worker kilo-worker

# /health must report the revision baked into the verified image.
echo "--- Commit verification ---"

health_commit=""
for attempt in $(seq 1 30); do
    health_commit=$(curl --fail --retry 2 --retry-delay 1 --retry-all-errors \
        --connect-timeout 5 --max-time 15 -sS \
        "https://ai.tusker.net.au/health" 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin).get('commit',''))" \
        || true)
    if [ "${health_commit}" = "${COMMIT}" ]; then
        break
    fi
    echo "Waiting for /health commit propagation: live=${health_commit:-unavailable} intended=${COMMIT} (attempt ${attempt}/30)"
    sleep 5
done

if [ "${health_commit}" != "${COMMIT}" ]; then
    echo "ERROR: /health commit mismatch — live ${health_commit:-unavailable}, intended ${COMMIT}"
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

# The tracked manifest is the durable record of what production runs. The build
# host has no usable git, so print the pin for the workstation to commit, or
# apply it in place when TUSKER_PIN_MANIFEST=1.
echo "--- Manifest pin ---"
PIN_ARGS=(--digest "${IMAGE_DIGEST}" --commit "${COMMIT}" --tag "${IMAGE_TAG}")
if [[ "${TUSKER_PIN_MANIFEST:-0}" == "1" ]]; then
    python3 "${SCRIPT_DIR}/pin-manifest.py" k8s/deployment.yaml "${PIN_ARGS[@]}" --write
    [[ -d .git ]] && git diff --stat -- k8s/deployment.yaml || true
else
    python3 "${SCRIPT_DIR}/pin-manifest.py" k8s/deployment.yaml "${PIN_ARGS[@]}"
    echo "Commit k8s/deployment.yaml from the workstation so git records what runs,"
    echo "then run k8s/verify-provenance.sh."
fi

echo "=== Done ==="
