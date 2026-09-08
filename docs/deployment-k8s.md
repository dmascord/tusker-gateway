# Kubernetes deployment: Tusker Gateway

Tusker Gateway runs alongside Hermes in the `hermes` namespace on the `visor` cluster.

## Source paths

| Where | Path |
|---|---|
| Local checkout | `~/dev/tusker-ai-gateway/` |
| Source on visor | `/srv/opencode/tusker-ai-gateway/` |
| Image registry | `registry.tusker.net.au:5000/tusker-gateway` |
| Manifests | `tusker-ai-gateway/k8s/` |

## 1. Sync source to visor

The build host is `visor`. Deploy a verified commit, not an rsync overlay of
uncommitted files. Synchronize an exported tree to a dedicated build directory
and pass the full local commit explicitly; visor's Git metadata is not authoritative.

```bash
REV=$(git rev-parse HEAD)
EXPORT=$(mktemp -d)
git archive "$REV" | tar -x -C "$EXPORT"
rsync -a --delete "$EXPORT/" "visor:/srv/opencode/tusker-ai-gateway-build-$REV/"
```

## 2. Build and deploy

The convenience script builds and pushes the image, renders the intended image
into a temporary deployment manifest, applies it once, and verifies the rollout.
`TUSKER_COMMIT` must be the explicit full source SHA. The image embeds it;
the deployment does not override it with an environment value.

```bash
ssh visor "cd /srv/opencode/tusker-ai-gateway-build-$REV && TUSKER_COMMIT=$REV ./k8s/deploy.sh $REV"
rm -r "$EXPORT"
```

Do not run concurrent deployments to the same gateway. Applying deployment or
config manifests requires operator authorization. The script verifies the
running ready pod's image digest against the pushed digest and checks the public
`/health` revision before reporting success.

## 3. Smoke test

The gateway is fronted by `ai.tusker.net.au` (same edge as Hermes). The deployment
script requires successful `/health` and `/ready` responses, then executes
`k8s/smoke_chat.py` against `/v1/chat/completions`.

The chat smoke requires a successful HTTP response, nonempty assistant content,
and a complete `[DONE]` stream without error or malformed events. HTTP errors,
truncated streams, missing credentials, and time/size limits fail deployment.
The gateway key is passed through `SMOKE_API_KEY`, not printed or passed on the
helper's command line. Do not replace this check with a truncated `curl` preview.

## 4. DNS

`ai.tusker.net.au` already points at the cluster LB. The gateway shares the
edge with Hermes — no DNS work needed for it.

## 5. Configuration

Tusker Gateway uses the isolated `tusker-env-vault` secret. The deployment
imports its provider keys with `envFrom` and declares the pool JSON inline in
`k8s/deployment.yaml` as `TUSKER_POOL_CODE`, `TUSKER_POOL_PRIVACY`,
`TUSKER_POOL_PREMIUM`, and `TUSKER_POOL_SWARM`.

OAuth credentials are separate pools: Codex uses `CODEX_CREDENTIALS` (or the
provider-specific `OPENCODE_CODEX_CREDENTIALS`), public Copilot uses
`GITHUB_COPILOT_CREDENTIALS`, and Enterprise Copilot uses
`GITHUB_COPILOT_ENTERPRISE_CREDENTIALS`. Keep these keys in
`tusker-env-vault`; a bearer API key alone does not populate an OAuth pool.

The gateway reads provider keys for every provider named in those pools:
- `OPENROUTER_API_KEY`
- `MINIMAX_API_KEY` (and similar for `minimax` typo alias, if any)
- `OLLAMA_API_KEY` / `OLLAMA_MAC_API_KEY` (for `ollama-cloud`)
- `OPENCODE_GO_API_KEY`
- `GROQ_API_KEY`
- `ARCEEAI_API_KEY`
- `GITHUB_COPILOT_*` (OAuth credential pools)
- `CEREBRAS_API_KEY`
- `GEMINI_API_KEY`
- `COHERE_API_KEY`
- `VOYAGE_API_KEY` (optional, for `/v1/rerank`)
- `JINA_API_KEY` (optional, for `/v1/rerank`)
- `ZAI_API_KEY`
- `XIAOMI_API_KEY`
- `NVIDIA_API_KEY` (retained in the isolated secret, but direct NVIDIA
  catalog discovery is disabled while its upstream capacity is saturated)
- `SYNTHETIC_API_KEY`

`POST /v1/rerank` is independent of chat-pool disablement. For example,
Cohere may remain excluded from chat pool/catalog construction while its
native rerank endpoint is still available when `COHERE_API_KEY` is present.
The gateway uses Cohere first, then configured Voyage/Jina keys, and exposes
the normalized `hermes-reranker` model alias.

Always verify pool providers exist in `tusker_gateway/config.py:DEFAULT_PROVIDER_REGISTRY`
before adding them — unknown providers raise `ProviderError("Unknown provider: ...")`
which becomes HTTP 502 and exhausts the agent retry budget.

When checking OMP routing, use the standalone provider configured as
`tusker-gateway` with `https://ai.tusker.net.au/v1`. The legacy
`hermes-gateway` provider's `/v1` requests continue to use the old hostname
for compatibility, but terminate at `tusker-gateway`; cross-host redirects
would cause some clients to drop `Authorization` on API POSTs. All non-API
Hermes traffic is redirected to the AI hostname. The legacy Hermes deployment
is scaled to zero after cutover; its PVC, service, and secrets remain intact as
a rollback target. Changes to this deployment's `TUSKER_POOL_CODE` therefore
apply to both API hostnames during the compatibility transition.

## 6. Rollback

```bash
kubectl -n hermes rollout undo deployment/tusker-gateway
```

## 7. Teardown

```bash
kubectl -n hermes delete pvc tusker-home-rwx
kubectl -n hermes delete pvc tusker-home
kubectl -n hermes delete service tusker-gateway
kubectl -n hermes delete deployment tusker-gateway
```

## Key differences from Hermes

| | Hermes | Tusker Gateway |
|---|---|---|
| PVC | `hermes-home` | `tusker-home-rwx` (ReadWriteMany; `tusker-home` retained as fall-back) |
| Image | `hermes-agent` | `tusker-gateway` |
| Host | `hermes.tusker.net.au` (API compatibility + redirect) | `ai.tusker.net.au` |
| PVC | `hermes-home` | `tusker-home` |
| Config | `hermes-env-vault` | `tusker-env-vault` (isolated) |

## Migration history

- **2026-08-21** — Migrated the three Codex OAuth credentials (`dmascord@gmail.com`,
  `damien.01@tusker.net.au`, `damien.02@tusker.net.au`) from
  `hermes.tusker.net.au` (deployment `hermes`) to `ai.tusker.net.au` (this
  gateway). The OAuth tokens are stored on the gateway's PVC at
  `/home/tusker/.hermes/auth.json` and rotated by the `CodexTokenRotator`.
  Hermes's pool was cleared to prevent refresh-token reuse collisions.
  `TUSKER_POOL_CODE` and `TUSKER_POOL_PRIVACY` were rebuilt from the live hermes
  pools, plus all `openai-codex/*` models exposed by the migrated catalog.
- **2026-09-04** — Cut the `hermes.tusker.net.au` `/v1/` API route over to
  `tusker-gateway` without changing DNS, OMP configuration, or the Hermes web
  routes. Merged the three legacy Hermes client-key identities into
  `tusker-env-vault` (seven unique gateway-accepted keys total), changed the
  deployment to consume `API_KEYS` from that secret, and added compatibility
  entries for the legacy model catalog.
- **2026-09-04** — Redirected non-API `hermes.tusker.net.au` traffic to
  `ai.tusker.net.au` on both Traefik entrypoints, retained a transparent
  `/v1` compatibility route to `tusker-gateway` so authenticated API POSTs do
  not lose their credentials across a host redirect, and scaled the legacy
  Hermes deployment to zero. The legacy PVC, service, and secrets remain
  available for rollback.
