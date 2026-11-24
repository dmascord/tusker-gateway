#!/usr/bin/env bash
set -euo pipefail

# Run from an operator-controlled terminal. Claude Code owns the OAuth flow:
# the login happens in the Claude worker (the pod that actually uses the
# credential), then the resulting .credentials.json is mirrored into the
# claude-cli-credentials Secret and the worker is rolled so every pod re-seeds
# it. Credentials and one-time codes must never be copied into chat, shell
# history, or logs.
namespace="${TUSKER_K8S_NAMESPACE:-hermes}"
deployment="${TUSKER_CLAUDE_DEPLOYMENT:-tusker-claude-worker}"
container="${TUSKER_CLAUDE_CONTAINER:-tusker-claude-worker}"
secret="${TUSKER_CLAUDE_SECRET:-claude-cli-credentials}"
credential_path="/home/tusker/.claude/.credentials.json"

tmp="$(mktemp "${TMPDIR:-/tmp}/claude-creds.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

printf 'Starting interactive Claude subscription login in %s/%s.\n' "$namespace" "$deployment"
printf 'Complete the browser step in this terminal. Claude Code stores the login in the worker runtime home.\n'
printf 'Do not paste access tokens, refresh tokens, or one-time codes into chat.\n'
kubectl -n "$namespace" exec -it "deployment/$deployment" -c "$container" -- \
    claude auth login --claudeai

# Read the new credential without printing it. Validate the shape before
# overwriting the Secret so a failed login cannot wipe a working token.
kubectl -n "$namespace" exec "deployment/$deployment" -c "$container" -- \
    cat "$credential_path" > "$tmp"
TMP_CREDS="$tmp" python3 -c '
import json, os
with open(os.environ["TMP_CREDS"], encoding="utf-8") as handle:
    doc = json.load(handle)
assert isinstance(doc.get("claudeAiOauth"), dict), "missing claudeAiOauth block"
assert doc["claudeAiOauth"].get("refreshToken"), "missing refresh token"
'

printf 'Updating Secret %s and re-seeding worker pods...\n' "$secret"
kubectl -n "$namespace" create secret generic "$secret" \
    --from-file=".credentials.json=$tmp" \
    --dry-run=client -o yaml \
    | kubectl -n "$namespace" apply -f -
kubectl -n "$namespace" rollout restart "deployment/$deployment"
kubectl -n "$namespace" rollout status "deployment/$deployment" --timeout=300s

printf '\nSanitized status after login:\n'
kubectl -n "$namespace" exec "deployment/$deployment" -c "$container" -- \
    claude auth status --json \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"loggedIn": d.get("loggedIn"), "authMethod": d.get("authMethod"), "loginExpiresAt": d.get("loginExpiresAt")}, sort_keys=True))'
