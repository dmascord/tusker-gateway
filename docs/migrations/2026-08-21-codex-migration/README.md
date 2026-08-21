# Codex OAuth migration: hermes.tusker.net.au → ai.tusker.net.au

Date: 2026-08-21

## What changed

Migrated three Codex OAuth credentials (`dmascord@gmail.com`, `damien.01@tusker.net.au`,
`damien.02@tusker.net.au`) from the `hermes-agent` deployment (served at
`hermes.tusker.net.au`) to the standalone `tusker-gateway` deployment (served at
`ai.tusker.net.au`).

Also rebuilt the gateway's pool configuration to match the live hermes pool
configuration, plus all `openai-codex/*` models exposed by the OAuth catalog.

## Files in this directory

- `hermes_pool_list_full.txt` — The raw pool contents extracted from
  `hermes.tusker.net.au` (48 code + 21 privacy models) via `kubectl exec` on
  `deployment/hermes` at the time of migration.
- `build_pools_yaml.py` — Python script that converts
  `hermes_pool_list_full.txt` into the `TUSKER_POOL_CODE` /
  `TUSKER_POOL_PRIVACY` JSON env values that go into `k8s/deployment.yaml`.
  Filters out providers the gateway doesn't support (`xiaomi`, `mlx-mac`,
  `ollama`, `ollama-mac`, `openai` — no `OPENAI_API_KEY`) and appends the six
  `openai-codex/*` models.
- `clear_hermes_codex.py` — Python script run inside the hermes pod that
  removes the `credential_pool.openai-codex` entries from
  `/home/tusker/.hermes/auth.json`. Writes an in-pod backup to
  `auth.json.bak.precodex-removal` before mutating.

## Result

| | hermes.tusker.net.au | ai.tusker.net.au |
|---|---|---|
| `credential_pool.openai-codex` | **0 entries** (cleared, backed up) | 3 entries (was already there, now actively used) |
| `hermes-code` / `TUSKER_POOL_CODE` pool | 48 candidate models | 49 candidate models |
| `hermes-privacy` / `TUSKER_POOL_PRIVACY` pool | 21 candidate models | 21 candidate models |
| `openai-codex/*` models reachable | Indirect (via `openai` prefix) | Direct (passthrough + pool) |

## Verification

End-to-end checks performed (all passed):

```
openai-codex/gpt-5.6-sol  -> chatcmpl-codex-1787274267635
openai-codex/gpt-5.6-terra -> chatcmpl-codex-1787274269433
openai-codex/gpt-5.6-luna  -> chatcmpl-codex-1787274271742
openai-codex/gpt-5.5       -> chatcmpl-codex-1787274273427
openai-codex/gpt-5.4       -> chatcmpl-codex-1787274274753
openai-codex/gpt-5.4-mini  -> chatcmpl-codex-1787274276442
```

## Backup locations

- **Hermes auth.json (before codex removal):**
  - Inside the hermes pod at `/home/tusker/.hermes/auth.json.bak.precodex-removal`
  - Locally at `~/dev/hermes/tmp/hermes-auth-pre-removal-20260821-110309.json`

## Rollback

If you need to revert the hermes side:

```bash
HERMES_POD=$(ssh visor 'kubectl -n hermes get pods -l app=hermes -o name | head -1' | sed 's|pod/||')
ssh visor "kubectl -n hermes exec $HERMES_POD -c hermes -- cp /home/tusker/.hermes/auth.json.bak.precodex-removal /home/tusker/.hermes/auth.json"
```

The gateway side can be reverted by reverting `k8s/deployment.yaml` to the
previous version of `TUSKER_POOL_CODE` / `TUSKER_POOL_PRIVACY`.
