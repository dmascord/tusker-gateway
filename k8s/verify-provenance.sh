#!/bin/bash
# Provenance drift check: does the tracked source match what the cluster runs?
#
# Compares the links that can drift apart:
#   1. tracked manifests  k8s/{deployment,worker-opencode,worker-claude}.yaml images
#   2. live Deployments   spec.template.spec.containers[0].image (digest or tag)
#   3. running pods       status.containerStatuses[].imageID
#   4. serving process    /health "commit"
# plus the Deployment's tusker.net.au/{commit,image-tag,image-digest} annotations,
# git HEAD, and the registry's current digest for the recorded tag - which is what
# catches a re-pushed tag, the failure mode where a restarted pod pulls a
# different binary than the one that passed the deploy smoke test.
# The registry name resolves only inside the cluster network, so tag lookups fall
# back to `ssh visor` unless PROV_REGISTRY_SSH is set (an empty value disables the
# fallback; digest-form references need no registry at all).
#
# Usage: k8s/verify-provenance.sh [--manifest PATH] [--health-url URL]
#            [--namespace NS] [--allow-unresolved] [--quiet]
# Exit: 0 every link agrees, 1 drift or unverifiable link, 2 bad usage.
# Env: KUBECTL_CONTEXT selects the cluster context (default: ambient),
#      KUBECTL_NAMESPACE the namespace, PROV_MANIFEST the tracked manifest,
#      PROV_HEALTH_URL the /health URL, PROV_REGISTRY_SSH the registry fallback.
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
PROV_REGISTRY=${PROV_REGISTRY:-registry.tusker.net.au:5000}
# shellcheck source=k8s/lib-provenance.sh
source "${SCRIPT_DIR}/lib-provenance.sh"
# Default the registry lookup to the build host, which sits on the cluster network.
PROV_REGISTRY_SSH=${PROV_REGISTRY_SSH-visor}

NAMESPACE=${KUBECTL_NAMESPACE:-hermes}
MANIFEST=${PROV_MANIFEST:-${REPO_ROOT}/k8s/deployment.yaml}
HEALTH_URL=${PROV_HEALTH_URL:-https://ai.tusker.net.au/health}
KUBECTL=${KUBECTL:-kubectl}
KUBECTL_CONTEXT=${KUBECTL_CONTEXT:-}
if [[ -n "${KUBECTL_CONTEXT}" ]]; then
    KUBECTL="${KUBECTL} --context ${KUBECTL_CONTEXT}"
fi
ALLOW_UNRESOLVED=0
QUIET=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --manifest) MANIFEST=$2; shift 2 ;;
        --health-url) HEALTH_URL=$2; shift 2 ;;
        --namespace) NAMESPACE=$2; shift 2 ;;
        --allow-unresolved) ALLOW_UNRESOLVED=1; shift ;;
        --quiet) QUIET=1; shift ;;
        -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
    esac
done

problems=""
warnings=""
note_problem() { problems="${problems}  x $1"$'\n'; }
note_warning() { warnings="${warnings}  ! $1"$'\n'; }
say() { [[ ${QUIET} == 1 ]] || printf '%s\n' "$*"; }
field() { grep -m1 -oE "$2" "$1" 2>/dev/null | tail -1; }

json_field() { # <json-on-stdin> <python-expression over dict `d` and annotations `a`>
    python3 -c '
import json
import sys

d = json.load(sys.stdin)
a = d["metadata"].get("annotations") or {}
t = d["spec"]["template"]["spec"]["containers"][0]
print(eval(sys.argv[1], {"d": d, "a": a, "t": t}))
' "$1"
}

pod_digests() { # <label-selector> -> "pod-name sha256:..." per container
    ${KUBECTL} -n "${NAMESPACE}" get pods -l "$1" -o json 2>/dev/null | python3 -c '
import json
import sys

for pod in json.load(sys.stdin)["items"]:
    if pod["metadata"].get("deletionTimestamp"):
        continue
    for status in pod.get("status", {}).get("containerStatuses", []) or []:
        image_id = status.get("imageID", "")
        if "@" in image_id:
            print(pod["metadata"]["name"], image_id.rsplit("@", 1)[-1])
' 2>/dev/null
}

# The gateway and both CLI workers run the same image with different entrypoints.
# A deploy that moves only one of them leaves the others on a stale digest, so
# every workload gets the same tracked/live/pod comparison.
WORKLOADS=(
    "tusker-gateway|${MANIFEST}"
    "tusker-opencode-worker|${REPO_ROOT}/k8s/worker-opencode.yaml"
    "tusker-claude-worker|${REPO_ROOT}/k8s/worker-claude.yaml"
)

# Parallel indexed arrays (bash 3.2 compatible: no associative arrays here).
W_NAMES=(); W_MANIFESTS=(); W_TRACKED_IMAGE=(); W_TRACKED_COMMIT=()
W_TRACKED_DIGEST=(); W_LIVE_IMAGE=(); W_LIVE_COMMIT=(); W_LIVE_TAG=()
W_LIVE_ANNOTATED_DIGEST=(); W_LIVE_DIGEST=(); W_TAG_DIGEST=()
W_POD_LIST=(); W_POD_DIGEST=(); W_POD_COUNT=(); W_POD_DISTINCT=()

for entry in "${WORKLOADS[@]}"; do
    name=${entry%%|*}
    manifest=${entry#*|}
    W_NAMES+=("${name}"); W_MANIFESTS+=("${manifest}")
    image=$(field "${manifest}" 'registry\.tusker\.net\.au:5000/tusker-gateway(@sha256:[0-9a-f]{64}|:[A-Za-z0-9._-]+)') || true
    commit=$(field "${manifest}" 'tusker\.net\.au/commit: [0-9a-f]{40}') || true
    commit=${commit##*: }
    W_TRACKED_IMAGE+=("${image}"); W_TRACKED_COMMIT+=("${commit}")
    td=""
    if [[ -n "${image}" ]]; then td=$(prov_image_digest "${image}") || true; fi
    W_TRACKED_DIGEST+=("${td}")

    deployment=$(${KUBECTL} -n "${NAMESPACE}" get deployment "${name}" -o json 2>/dev/null) || true
    li=""; lc=""; lt=""; lad=""
    if [[ -n "${deployment}" ]]; then
        li=$(printf '%s' "${deployment}" | json_field 't["image"]') || true
        lc=$(printf '%s' "${deployment}" | json_field 'a.get("tusker.net.au/commit", "")') || true
        lt=$(printf '%s' "${deployment}" | json_field 'a.get("tusker.net.au/image-tag", "")') || true
        lad=$(printf '%s' "${deployment}" | json_field 'a.get("tusker.net.au/image-digest", "")') || true
    else
        note_problem "cannot read Deployment ${NAMESPACE}/${name} (context $(kubectl config current-context 2>/dev/null || echo '<unknown>'))"
    fi
    W_LIVE_IMAGE+=("${li}"); W_LIVE_COMMIT+=("${lc}")
    W_LIVE_TAG+=("${lt}"); W_LIVE_ANNOTATED_DIGEST+=("${lad}")
    ld=""; if [[ -n "${li}" ]]; then ld=$(prov_image_digest "${li}") || true; fi
    W_LIVE_DIGEST+=("${ld}")
    rd=""; if [[ -n "${lt}" ]]; then rd=$(prov_registry_digest "${lt}") || true; fi
    W_TAG_DIGEST+=("${rd}")

    pods=$(pod_digests "app=${name}")
    W_POD_LIST+=("${pods}")
    W_POD_DIGEST+=("$(printf '%s\n' "${pods}" | awk 'NF{print $2; exit}')")
    W_POD_COUNT+=("$(printf '%s\n' "${pods}" | grep -c 'sha256:' || true)")
    W_POD_DISTINCT+=("$(printf '%s\n' "${pods}" | awk 'NF{print $2}' | sort -u | grep -c . || true)")
done

# Anchors retained for the commit chain and final summary.
gateway_digest=${W_POD_DIGEST[0]}
tracked_image=${W_TRACKED_IMAGE[0]}
tracked_commit=${W_TRACKED_COMMIT[0]}
live_commit=${W_LIVE_COMMIT[0]}

health_commit=$(curl -s -m 10 --retry 2 --retry-delay 1 --retry-all-errors "${HEALTH_URL}" 2>/dev/null \
    | python3 -c 'import json
import sys

try:
    print(json.load(sys.stdin).get("commit", ""))
except Exception:
    print("")' 2>/dev/null) || true

git_head=""
if [[ -d "${REPO_ROOT}/.git" ]]; then
    git_head=$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null) || true
fi
# The running revision must be in HEAD's history. A manifest- or docs-only commit
# is a legitimate difference between git HEAD and the deployed commit; an
# unreleased build is not.
ahead_of_deployed=""
if [[ -n "${git_head}" && -n "${health_commit}" ]]; then
    ahead_of_deployed=$(git -C "${REPO_ROOT}" rev-list --count "${health_commit}..${git_head}" 2>/dev/null) || ahead_of_deployed=""
fi

say "=== Provenance: tracked manifests vs ${NAMESPACE} deployments ==="
say "$(printf '  %-28s %s' 'kubectl context' "${KUBECTL_CONTEXT:-$(kubectl config current-context 2>/dev/null || echo '<none>')}")"

for i in "${!W_NAMES[@]}"; do
    name=${W_NAMES[$i]}
    say ""
    say "--- ${name} ---"
    say "$(printf '  %-28s %s' 'tracked manifest' "${W_TRACKED_IMAGE[$i]:-<none>}")"
    say "$(printf '  %-28s %s' 'tracked digest' "${W_TRACKED_DIGEST[$i]:-<none>}")"
    say "$(printf '  %-28s %s' 'live Deployment image' "${W_LIVE_IMAGE[$i]:-<none>}")"
    say "$(printf '  %-28s %s' 'live Deployment digest' "${W_LIVE_DIGEST[$i]:-<none>}")"
    say "$(printf '  %-28s %s -> %s' 'recorded tag' "${W_LIVE_TAG[$i]:-<none>}" "${W_TAG_DIGEST[$i]:-<unresolved>}")"
    say "$(printf '  %-28s %s' 'Deployment digest annotation' "${W_LIVE_ANNOTATED_DIGEST[$i]:-<none>}")"
    say "$(printf '  %-28s %s (%s pod(s), %s distinct)' 'pods' "$(printf '%s' "${W_POD_LIST[$i]}" | tr '\n' ' ')" "${W_POD_COUNT[$i]}" "${W_POD_DISTINCT[$i]}")"

    ti=${W_TRACKED_IMAGE[$i]}
    if [[ -n "${ti}" && "${ti}" != *@sha256:* ]]; then
        note_problem "${name}: tracked manifest pins the tag ${ti}, not a digest (run k8s/pin-manifest.py)"
    fi
    pd=${W_POD_DIGEST[$i]}
    if [[ -n "${pd}" ]]; then
        for pair in "tracked manifest:${W_TRACKED_DIGEST[$i]}" "live Deployment:${W_LIVE_DIGEST[$i]}" \
                    "recorded tag:${W_TAG_DIGEST[$i]}" "Deployment annotation:${W_LIVE_ANNOTATED_DIGEST[$i]}"; do
            label=${pair%%:*}
            value=${pair#*:}
            if [[ -z "${value}" ]]; then
                note_warning "${name} ${label}: digest unknown"
            elif [[ "${value}" != "${pd}" ]]; then
                note_problem "${name} ${label} digest ${value} != running ${pd}"
            fi
        done
        if [[ "${W_POD_DISTINCT[$i]}" != "1" ]]; then
            note_warning "${name}: ${W_POD_DISTINCT[$i]} distinct pod digests (mid-rollout?)"
        fi
    else
        note_problem "${name}: no pod imageID to anchor the digest chain"
    fi
done

say ""
say "commit chain"
say "$(printf '  %-28s %s' '/health' "${health_commit:-<unreachable>}")"
say "$(printf '  %-28s %s' 'git HEAD' "${git_head:-<no .git>}${ahead_of_deployed:+ (${ahead_of_deployed} commit(s) ahead of the running revision)}")"
for i in "${!W_NAMES[@]}"; do
    name=${W_NAMES[$i]}
    say "$(printf '  %-24s %s' "${name} Deployment" "${W_LIVE_COMMIT[$i]:-<none>}")"
    say "$(printf '  %-24s %s' "${name} manifest" "${W_TRACKED_COMMIT[$i]:-<none>}")"
done

if [[ -n "${health_commit}" ]]; then
    for i in "${!W_NAMES[@]}"; do
        name=${W_NAMES[$i]}
        for pair in "Deployment annotation:${W_LIVE_COMMIT[$i]}" "manifest annotation:${W_TRACKED_COMMIT[$i]}"; do
            label=${pair%%:*}
            value=${pair#*:}
            if [[ -z "${value}" ]]; then
                note_warning "${name} ${label}: no commit recorded"
            elif [[ "${value}" != "${health_commit}" ]]; then
                note_problem "${name} ${label} ${value} != /health ${health_commit}"
            fi
        done
    done
else
    note_problem "/health at ${HEALTH_URL} reported no commit"
fi
if [[ -n "${health_commit}" ]]; then
    if [[ -z "${git_head}" ]]; then
        note_warning "no .git: cannot confirm the running revision is committed"
    elif ! git -C "${REPO_ROOT}" merge-base --is-ancestor "${health_commit}" "${git_head}" 2>/dev/null; then
        note_problem "running revision ${health_commit} is not in the history of git HEAD ${git_head}"
    fi
fi

if [[ -n "${warnings}" ]]; then
    say ""
    say "unverified"
    printf '%s' "${warnings}"
fi

if [[ -n "${problems}" ]]; then
    say ""
    say "DRIFT"
    printf '%s' "${problems}"
    if [[ ${ALLOW_UNRESOLVED} == 1 ]]; then
        say ""
        say "--allow-unresolved: not failing"
        exit 0
    fi
    exit 1
fi

say ""
say "OK: all tracked manifests, live Deployments, running pods and /health agree"
exit 0