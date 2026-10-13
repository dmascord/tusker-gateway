#!/usr/bin/env bash
set -euo pipefail

# Run an authenticated production smoke without copying the gateway credential
# to the workstation, a file, or the process command line.
#
# The key is read inside the visor shell from the Kubernetes Secret, held only
# in that shell's environment for smoke_chat.py, and never printed.

if [[ -n "${TUSKER_PROD_SSH_HOST+x}" ]]; then
    SSH_HOST="$TUSKER_PROD_SSH_HOST"
    SSH_PORT="${TUSKER_PROD_SSH_PORT:-8022}"
    SSH_COMMAND=(ssh -T -p "$SSH_PORT" -o BatchMode=yes "$SSH_HOST")
elif ssh -T -o BatchMode=yes -o ConnectTimeout=8 visor true 2>/dev/null; then
    # Prefer the configured visor alias when direct cluster access works. The
    # external SSH endpoint is a fallback for networks that cannot resolve it.
    SSH_HOST="visor"
    SSH_PORT=""
    SSH_COMMAND=(ssh -T -o BatchMode=yes visor)
else
    SSH_HOST="[REDACTED-EMAIL]"
    SSH_PORT="8022"
    SSH_COMMAND=(ssh -T -p "$SSH_PORT" -o BatchMode=yes "$SSH_HOST")
fi
NAMESPACE="${TUSKER_PROD_NAMESPACE:-hermes}"
SECRET="${TUSKER_PROD_SECRET:-tusker-env-vault}"
REMOTE_ROOT="${TUSKER_PROD_ROOT:-/srv/opencode/tusker-ai-gateway}"
MODEL="hermes-privacy"
TIMEOUT="120"
WITH_MEDIA=0

usage() {
    printf 'Usage: %s [--model MODEL] [--timeout SECONDS] [--with-media]\n' "$0"
}

while (($#)); do
    case "$1" in
        --model)
            (($# >= 2)) || { usage >&2; exit 2; }
            MODEL=$2
            shift 2
            ;;
        --timeout)
            (($# >= 2)) || { usage >&2; exit 2; }
            TIMEOUT=$2
            shift 2
            ;;
        --with-media)
            WITH_MEDIA=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done

[[ "$MODEL" =~ ^[A-Za-z0-9._:/-]+$ ]] || {
    echo "ERROR: invalid model name" >&2
    exit 2
}
[[ "$TIMEOUT" =~ ^[1-9][0-9]*([.][0-9]+)?$ ]] || {
    echo "ERROR: timeout must be positive" >&2
    exit 2
}

REMOTE_SMOKE="$REMOTE_ROOT/k8s/smoke_chat.py"
MEDIA_ARG="--skip-media"
if ((WITH_MEDIA)); then
    MEDIA_ARG=""
fi

# Deliberately keep the secret retrieval and use on visor. Do not replace this
# with command substitution on the workstation or add shell tracing here.
"${SSH_COMMAND[@]}" \
    "NAMESPACE='$NAMESPACE' SECRET='$SECRET' SMOKE='$REMOTE_SMOKE' MODEL='$MODEL' TIMEOUT='$TIMEOUT' MEDIA_ARG='$MEDIA_ARG' bash -s" <<'REMOTE'
set -euo pipefail
key=$(kubectl -n "$NAMESPACE" get secret "$SECRET" \
    -o jsonpath='{.data.API_KEYS}' | base64 -d | cut -d, -f1)
if [[ -z "$key" ]]; then
    echo "ERROR: production API_KEYS secret is empty or unreadable" >&2
    exit 1
fi
if [[ -n "$MEDIA_ARG" ]]; then
    SMOKE_API_KEY="$key" python3 "$SMOKE" \
        --url https://ai.tusker.net.au/v1/chat/completions \
        --model "$MODEL" --timeout "$TIMEOUT" "$MEDIA_ARG"
else
    SMOKE_API_KEY="$key" python3 "$SMOKE" \
        --url https://ai.tusker.net.au/v1/chat/completions \
        --model "$MODEL" --timeout "$TIMEOUT"
fi
unset key SMOKE_API_KEY
REMOTE
