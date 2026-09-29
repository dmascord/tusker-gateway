#!/bin/bash
# Provenance drift check: does the tracked source match what the cluster runs?
#
# Compares the links that can drift apart:
#   1. tracked manifest   k8s/deployment.yaml image (repo@sha256:<digest>)
#   2. live Deployment    spec.template.spec.containers[0].image (digest or tag)
#   3. running pod        status.containerStatuses[].imageID
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

tracked_image=$(field "${MANIFEST}" 'registry\.tusker\.net\.au:5000/tusker-gateway(@sha256:[0-9a-f]{64}|:[A-Za-z0-9._-]+)') || true
tracked_commit=$(field "${MANIFEST}" 'tusker\.net\.au/commit: [0-9a-f]{40}') || true
tracked_commit=${tracked_commit##*: }
tracked_digest=""
if [[ -n "${tracked_image}" ]]; then
    tracked_digest=$(prov_image_digest "${tracked_image}") || true
fi

deployment=$(${KUBECTL} -n "${NAMESPACE}" get deployment tusker-gateway -o json 2>/dev/null) || true
live_image=""; live_commit=""; live_tag=""; live_annotated_digest=""
if [[ -n "${deployment}" ]]; then
    live_image=$(printf '%s' "${deployment}" | json_field 't["image"]') || true
    live_commit=$(printf '%s' "${deployment}" | json_field 'a.get("tusker.net.au/commit", "")') || true
    live_tag=$(printf '%s' "${deployment}" | json_field 'a.get("tusker.net.au/image-tag", "")') || true
    live_annotated_digest=$(printf '%s' "${deployment}" | json_field 'a.get("tusker.net.au/image-digest", "")') || true
fi
if [[ -z "${deployment}" ]]; then
    note_problem "cannot read Deployment ${NAMESPACE}/tusker-gateway (context $(kubectl config current-context 2>/dev/null || echo '<unknown>'))"
fi
live_digest=""
if [[ -n "${live_image}" ]]; then
    live_digest=$(prov_image_digest "${live_image}") || true
fi
tag_digest=""
if [[ -n "${live_tag}" ]]; then
    tag_digest=$(prov_registry_digest "${live_tag}") || true
fi

gateway_pods=$(pod_digests app=tusker-gateway)
kilo_pods=$(pod_digests app=tusker-kilo-worker)
gateway_digest=$(printf '%s\n' "${gateway_pods}" | awk 'NF{print $2; exit}')
kilo_digest=$(printf '%s\n' "${kilo_pods}" | awk 'NF{print $2; exit}')
gateway_pod_count=$(printf '%s\n' "${gateway_pods}" | grep -c 'sha256:' || true)
gateway_distinct=$(printf '%s\n' "${gateway_pods}" | awk 'NF{print $2}' | sort -u | grep -c . || true)
kilo_pod_count=$(printf '%s\n' "${kilo_pods}" | grep -c 'sha256:' || true)

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

say "=== Provenance: ${MANIFEST} vs ${NAMESPACE}/tusker-gateway ==="
say "$(printf '  %-28s %s' 'kubectl context' "${KUBECTL_CONTEXT:-$(kubectl config current-context 2>/dev/null || echo '<none>')}")"
say ""
say "image digest chain"
say "$(printf '  %-28s %s' 'tracked manifest' "${tracked_image:-<none>}")"
say "$(printf '  %-28s %s' 'tracked digest' "${tracked_digest:-<none>}")"
say "$(printf '  %-28s %s' 'live Deployment image' "${live_image:-<none>}")"
say "$(printf '  %-28s %s' 'live Deployment digest' "${live_digest:-<none>}")"
say "$(printf '  %-28s %s -> %s' 'recorded tag' "${live_tag:-<none>}" "${tag_digest:-<unresolved>}")"
say "$(printf '  %-28s %s' 'Deployment digest annotation' "${live_annotated_digest:-<none>}")"
say "$(printf '  %-28s %s (%s pod(s), %s distinct)' 'gateway pods' "$(printf '%s' "${gateway_pods}" | tr '\n' ' ')" "${gateway_pod_count}" "${gateway_distinct}")"
say "$(printf '  %-28s %s (%s pod(s))' 'kilo-worker pods' "$(printf '%s' "${kilo_pods}" | tr '\n' ' ')" "${kilo_pod_count}")"
say ""
say "commit chain"
say "$(printf '  %-28s %s' '/health' "${health_commit:-<unreachable>}")"
say "$(printf '  %-28s %s' 'git HEAD' "${git_head:-<no .git>}${ahead_of_deployed:+ (${ahead_of_deployed} commit(s) ahead of the running revision)}")"
say "$(printf '  %-28s %s' 'Deployment annotation' "${live_commit:-<none>}")"
say "$(printf '  %-28s %s' 'manifest annotation' "${tracked_commit:-<none>}")"

if [[ -n "${tracked_image}" && "${tracked_image}" != *@sha256:* ]]; then
    note_problem "tracked manifest pins the tag ${tracked_image}, not a digest (run k8s/pin-manifest.py)"
fi

if [[ -n "${gateway_digest}" ]]; then
    for pair in "tracked manifest:${tracked_digest}" "live Deployment:${live_digest}" \
                "recorded tag:${tag_digest}" "Deployment annotation:${live_annotated_digest}" \
                "kilo-worker pod:${kilo_digest}"; do
        name=${pair%%:*}
        value=${pair#*:}
        if [[ -z "${value}" ]]; then
            note_warning "${name}: digest unknown"
        elif [[ "${value}" != "${gateway_digest}" ]]; then
            note_problem "${name} digest ${value} != running gateway ${gateway_digest}"
        fi
    done
    if [[ "${gateway_distinct}" != "1" ]]; then
        note_warning "${gateway_distinct} distinct gateway pod digests (mid-rollout?)"
    fi
else
    note_problem "no gateway pod imageID to anchor the digest chain"
fi

if [[ -n "${health_commit}" ]]; then
    for pair in "Deployment annotation:${live_commit}" "manifest annotation:${tracked_commit}"; do
        name=${pair%%:*}
        value=${pair#*:}
        if [[ -z "${value}" ]]; then
            note_warning "${name}: no commit recorded"
        elif [[ "${value}" != "${health_commit}" ]]; then
            note_problem "${name} ${value} != /health ${health_commit}"
        fi
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
say "OK: tracked manifest, live Deployment, running pods and /health all agree"
exit 0