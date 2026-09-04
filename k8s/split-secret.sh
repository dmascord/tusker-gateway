#!/bin/bash
# Split hermes-env-vault → tusker-env-vault in the hermes namespace.
#
# The Tusker gateway has been sharing hermes-env-vault for convenience,
# which mixes concerns: a Hermes key change can affect the gateway and
# vice versa, and the gateway currently pulls in ~200 Hermes-only keys
# (HERMES_CODE_FALLBACK_*, MCP_EMBED_API_KEY, CLOUDFLARE_DNS_API_TOKEN,
# ...) it never reads. This script creates a new secret with only the
# keys the gateway actually consumes, and prints a kubectl patch hint
# to switch k8s/deployment.yaml to use it.
#
# Usage:
#   ./k8s/split-secret.sh                # create tusker-env-vault
#   ./k8s/split-secret.sh --dry-run      # show what would be created
#
# Requires: kubectl context pointed at the cluster.

set -euo pipefail

NAMESPACE=hermes
SOURCE_SECRET=hermes-env-vault
TARGET_SECRET=tusker-env-vault

# Keys the gateway actually consumes, sourced from k8s/deployment.yaml
# envFrom + secretKeyRef + the literal API_KEYS entry.
KEYS=(
    # Provider API keys (one per upstream we talk to).
    "COPILOT_GITHUB_TOKEN"
    "OPENROUTER_API_KEY"
    "GROQ_API_KEY"
    "ARCEEAI_API_KEY"
    "GLM_API_KEY"
    "GEMINI_API_KEY"
    "CEREBRAS_API_KEY"
    "COHERE_API_KEY"
    "MINIMAX_API_KEY"
    "SYNTHETIC_API_KEY"
    "OLLAMA_API_KEY"
    "NVIDIA_API_KEY"
    "OPENCODE_GO_API_KEY"
    "OPENCODE_ZEN_API_KEY"
    "XIAOMI_API_KEY"
    "ZAI_API_KEY"
    # Credential pools referenced by the deployment. These must remain
    # separate so Codex, public Copilot, and Enterprise Copilot never share
    # the wrong OAuth credential set.
    "CODEX_CREDENTIALS"
    "OPENCODE_CODEX_CREDENTIALS"
    "GITHUB_COPILOT_CREDENTIALS"
    "GITHUB_COPILOT_ENTERPRISE_CREDENTIALS"
    "GITHUB_COPILOT_ENTERPRISE_TOKEN"
    # Hugging Face — used by sentence-transformers/all-MiniLM-L6-v2 to
    # silence the unauthenticated-Hub warning + lift rate limits.
    # Sourced from the repo .env (HF_TOKEN preferred, HF_API_KEY fallback).
    "HF_TOKEN"
    # Gateway's own bearer tokens (clients call Tusker with these).
    "API_KEYS"
)

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi
# Resolve the value for a single key. API_KEYS normally lives in the target
# secret now; fall back to the deployment literal for an initial split from
# an older checkout. Everything else is in hermes-env-vault, with a fallback
# to the repo .env file for keys not (yet) seeded there (e.g. HF_TOKEN ↔
# .env's HF_API_KEY).
resolve_value() {
    local k="$1"
    local primary=""
    if [[ "$k" == "API_KEYS" ]]; then
        primary=$(kubectl -n "$NAMESPACE" get secret "$TARGET_SECRET" \
            -o jsonpath="{.data.API_KEYS}" 2>/dev/null \
            | base64 -d 2>/dev/null || true)
    fi
    if [[ -z "$primary" && "$k" == "API_KEYS" ]]; then
        primary=$(kubectl -n "$NAMESPACE" get deploy tusker-gateway \
            -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="API_KEYS")].value}' \
            2>/dev/null || true)
    elif [[ "$k" != "API_KEYS" ]]; then
        primary=$(kubectl -n "$NAMESPACE" get secret "$SOURCE_SECRET" \
            -o jsonpath="{.data.$k}" 2>/dev/null \
            | base64 -d 2>/dev/null || true)
    fi
    if [[ -n "$primary" ]]; then
        echo "$primary"
        return
    fi
    # Fallback to repo .env when the source secret lacks the key.
    local env_file="${REPO_ROOT:-$(dirname "$(dirname "$(readlink -f "$0")")")}/.env"
    if [[ -f "$env_file" ]]; then
        # Try exact key first, then the common HF_TOKEN ↔ HF_API_KEY alias.
        local line
        line=$(grep -E "^${k}=" "$env_file" 2>/dev/null | head -1 \
            || true)
        if [[ -z "$line" && "$k" == "HF_TOKEN" ]]; then
            line=$(grep -E "^HF_API_KEY=" "$env_file" 2>/dev/null | head -1 || true)
        fi
        if [[ -n "$line" ]]; then
            echo "${line#*=}"
        fi
    fi
}

ARGS=()
for k in "${KEYS[@]}"; do
    VAL=$(resolve_value "$k")
    if [[ -z "$VAL" ]]; then
        echo "WARNING: $SOURCE_SECRET/$k not found or empty — skipping" >&2
        continue
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  would copy: $k (${#VAL} bytes)"
    else
        ARGS+=(--from-literal="$k=$VAL")
    fi
done

if [[ "$DRY_RUN" == "1" ]]; then
    echo
    echo "Dry run — would create secret $TARGET_SECRET in namespace $NAMESPACE"
    echo "with the keys listed above."
    exit 0
fi

if [[ ${#ARGS[@]} -eq 0 ]]; then
    echo "ERROR: no keys resolved — aborting" >&2
    exit 1
fi

echo "Creating secret $TARGET_SECRET in namespace $NAMESPACE..."
kubectl -n "$NAMESPACE" create secret generic "$TARGET_SECRET" "${ARGS[@]}" \
    --dry-run=client -o yaml | kubectl apply -f -

echo
echo "Done. To switch the deployment over:"
echo
echo "  1. Edit k8s/deployment.yaml:"
echo "     - envFrom: secretRef: name: hermes-env-vault"
echo "       →      envFrom: secretRef: name: tusker-env-vault"
echo "     - All secretKeyRef entries: name: hermes-env-vault"
echo "       →                          name: tusker-env-vault"
echo "  2. Re-run ./k8s/deploy.sh"
echo "  3. After the gateway is verified healthy, consider removing the"
echo "     unused keys from hermes-env-vault (Tusker no longer reads them)."
