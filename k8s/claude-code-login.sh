#!/usr/bin/env bash
set -euo pipefail

# Run from an operator-controlled terminal. Claude Code owns the OAuth flow
# and writes credentials to the gateway's mounted runtime home; credentials
# and one-time codes must never be copied into chat, shell history, or logs.
namespace="${TUSKER_K8S_NAMESPACE:-hermes}"
deployment="${TUSKER_K8S_DEPLOYMENT:-tusker-gateway}"
container="${TUSKER_K8S_CONTAINER:-tusker-gateway}"

printf 'Starting interactive Claude subscription login in %s/%s.\n' "$namespace" "$deployment"
printf 'Complete the browser step in this terminal. Claude Code stores the login in its mounted runtime home.\n'
printf 'Do not paste access tokens, refresh tokens, or one-time codes into chat.\n'
kubectl -n "$namespace" exec -it "deployment/$deployment" -c "$container" -- \
    claude auth login --claudeai

printf '\nSanitized status after login:\n'
kubectl -n "$namespace" exec "deployment/$deployment" -c "$container" -- \
    claude auth status --json \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"loggedIn": d.get("loggedIn"), "authMethod": d.get("authMethod")}, sort_keys=True))'
