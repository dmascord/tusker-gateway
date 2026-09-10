# OpenCode on Windows: connect to ai.tusker.net.au

How to point [OpenCode](https://opencode.ai) running on Windows at the Tusker
AI Gateway (`https://ai.tusker.net.au/v1`, OpenAI-compatible), with
configuration that survives OpenCode upgrades.

## Why this survives upgrades

OpenCode upgrades (npm `opencode upgrade`, the install script, or MSI) replace
only the binary. They never touch:

| Path | Purpose |
|---|---|
| `%USERPROFILE%\.config\opencode\opencode.json` | Global config (providers, default model) |
| `%USERPROFILE%\.local\share\opencode\auth.json` | Stored credentials (`/connect`) |

Put the provider config in the **global** config file and store the API key in
`auth.json` (or a user environment variable). Do **not** put the key in a
project-level `opencode.json` you check into Git, and do not rely on anything
inside the OpenCode install directory.

## 1. Get an API key

Keys are issued by the gateway operators. Store it in a user environment
variable so it is not pasted into config files:

```powershell
[Environment]::SetEnvironmentVariable("TUSKER_API_KEY", "<your-key>", "User")
```

Open a **new** terminal afterwards (already-open shells do not see new user
variables). The variable persists across reboots and upgrades.

## 2. Global config

Create/edit `%USERPROFILE%\.config\opencode\opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "tusker/hermes-code",
  "provider": {
    "tusker": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Tusker AI Gateway",
      "options": {
        "baseURL": "https://ai.tusker.net.au/v1",
        "apiKey": "{env:TUSKER_API_KEY}"
      },
      "models": {
        "hermes-code": { "name": "Hermes Code", "attachment": true, "modalities": { "input": ["text", "image"] } },
        "hermes-privacy": { "name": "Hermes Privacy (ZDR)", "attachment": true, "modalities": { "input": ["text", "image"] } },
        "hermes-premium": { "name": "Hermes Premium", "attachment": true, "modalities": { "input": ["text", "image"] } },
        "tusker-gateway": { "name": "Tusker (auto-routing)", "attachment": true, "modalities": { "input": ["text", "image"] } }
      }
    }
  }
}
```

Notes:

- `tusker` is an arbitrary provider ID; the `npm` package
  `@ai-sdk/openai-compatible` is required for any OpenAI-compatible endpoint.
- Model keys must exactly match IDs from `GET /v1/models`
  (`curl -H "Authorization: Bearer $TUSKER_API_KEY" https://ai.tusker.net.au/v1/models`).
- All listed models are virtual auto-routing aliases: the gateway picks a
  backend per request. `hermes-code` rotates through cheap coding models,
  `hermes-privacy` uses ZDR-only backends, `hermes-premium` uses heavyweight
  models, and `tusker-gateway` mixes across all pools.
- `{env:TUSKER_API_KEY}` substitution reads the user environment variable at
  startup.
- `"attachment": true` and `"modalities": { "input": ["text", "image"] }` enable
  image attachment support. Without these, OpenCode blocks images client-side
  with "this model does not support image input" even if the gateway and
  upstream model fully support vision.

## 3. Verify

```powershell
# Catalog reachability (should print model IDs)
curl.exe -s https://ai.tusker.net.au/v1/models -H "Authorization: Bearer $env:TUSKER_API_KEY"

# Chat round-trip (should print a JSON completion)
curl.exe -s https://ai.tusker.net.au/v1/chat/completions `
  -H "Authorization: Bearer $env:TUSKER_API_KEY" `
  -H "Content-Type: application/json" `
  -d '{"model":"tusker-gateway","messages":[{"role":"user","content":"Say READY"}],"max_tokens":10}'

# OpenCode: model picker should list the Tusker provider entries
opencode
# then run /models inside the TUI
```

If OpenCode shows no Tusker models: check `opencode.json` is valid JSON
(no trailing commas), the env var is set in the shell you launched from, and
the model keys match `/v1/models` exactly.

## 4. Alternative: store the key via /connect

If you prefer not to keep the key in an env var, run `/connect` in the OpenCode
TUI, choose the custom-provider entry, and paste the key — it lands in
`%USERPROFILE%\.local\share\opencode\auth.json`, which upgrades also preserve.
When using `/connect`, remove the `apiKey` line from the config (a config
`apiKey` takes precedence and would override `auth.json`).

## 5. Per-project overrides (optional)

A project-root `opencode.json` overrides the global config per project (e.g.
pin `hermes-privacy` for work on sensitive repos). Project config is merged
over global, so only the keys you set change. Project files are not part of
the upgrade path, but they are also not the persistent base — keep the
provider definition global and only override `model` per project.

## Operations

- **Rotate the key**: gateway operators append/replace the key in the cluster
  secret `tusker-env-vault` (`API_KEYS`, comma-separated), then
  `kubectl -n hermes rollout restart deployment/tusker-gateway`. Update the
  env var on the Windows machine and restart OpenCode.
- **No secrets in Git**: never commit the key; the `{env:...}` pattern exists
  for that reason.
- **Adding models**: `GET /v1/models` is the source of truth. Prefer virtual
  aliases (`tusker-gateway`, `hermes-code`, `hermes-privacy`) so backend
  routing changes don't require client config edits.
