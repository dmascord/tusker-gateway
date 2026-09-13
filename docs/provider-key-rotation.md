# Provider key rotation

The gateway stores encrypted provider API keys in the DB-backed config store
(`tusker_config_provider_api_keys`, encrypted via pgcrypto with
`TUSKER_KEY_ENCRYPTION_KEY`). Rotating a key is therefore a single-row DB
update followed by a config-generation bump — no pod restart required.

## Quick rotation (DB-only)

```bash
# From a host with the gateway's DB credentials and TUSKER_KEY_ENCRYPTION_KEY.
export TUSKER_STATE_DATABASE_URL='postgresql://...'
export TUSKER_KEY_ENCRYPTION_KEY='...'   # same secret the gateway pod uses

python -m tusker_gateway.tools.rotate_provider_key google NEW_GEMINI_KEY
```

Output:

```
rotated provider=google last4=abcd fingerprint=4aeb9910c09baccd... generation 62 -> 63
```

The gateway's `ConfigRuntime` watcher detects the generation bump, reloads the
config, decrypts the new key with `pgp_sym_decrypt`, and the next chat request
to `google::*` uses it. No pod restart; no rolling deploy; no downtime.

## Fallback: update the Secret (env-var fallback path)

If the DB config store is unavailable (the gateway falls back to env-var
configuration), edit `tusker-env-vault` directly:

```bash
# Patch the Gemini key in the shared gateway secret.
kubectl -n hermes create secret generic tusker-env-vault \
    --from-literal=GEMINI_API_KEY=NEW_KEY \
    --dry-run=client -o yaml | kubectl apply -f -
```

Then trigger a config reload — the simplest is to bump the DB
`generation` (even a no-op), or restart the gateway pod:

```bash
kubectl -n hermes rollout restart deployment/tusker-gateway
```

## Which providers can use DB-only rotation?

Any provider whose key is sourced from `tusker_config_provider_api_keys`
works without a deploy. The DB-backed store currently holds keys for:

- `groq`, `zai`, `xiaomi`, `gemini`/`google`, `synthetic`, `alibaba`,
  `ollama`, `opencode-zen`, `opencode-go`, `github-copilot`,
  `github-copilot-enterprise`, `cohere`, `nvidia`, `cerebras`, `minimax`,
  `workers-ai`.

OAuth pools (`openai-codex`, `github-copilot`, `github-copilot-enterprise`)
have their credentials in `tusker_config_oauth_credentials` and are
refreshed automatically by the `CodexTokenRotator` — they do not need
manual rotation unless refresh-token reuse fails.

## Audit trail

Each rotation appends an audit row to the `tusker_config_provider_api_keys`
table with:

- `provider` — the normalized provider name
- `last4` — last 4 characters of the new key
- `fingerprint` — sha256 of the new key (first 16 chars logged)
- `updated_at` — timestamp

The encrypted key is never logged in plaintext.
