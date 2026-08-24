# Provider Key Audit — 2026-08-24

Audit of every API key in the `hermes-env-vault` Kubernetes secret against the
configured provider endpoints in `tusker_gateway/config.py`. The goal was to
catch any `ProviderConfig(base_url=..., chat_path=...)` pair that doesn't match
the actual API for the key the secret holds.

## Method

For each secret key, every plausible endpoint was probed with `GET /v1/models`
and `POST /v1/chat/completions` (or the OpenAI-compatible analogue). The
registry endpoint is the one currently configured for that provider.

## Findings

### Fixed

| Provider | Before | After | Root cause |
|----------|--------|-------|------------|
| `xiaomi` | `https://api.xiaomimimo.com` (pay-as-you-go, 401) | `https://token-plan-sgp.xiaomimimo.com` | `XIAOMI_API_KEY` has `tp-` prefix — Xiaomi Token Plan key. Token Plan keys are region-specific; only the Singapore cluster accepts this key. |
| `zai` | `https://api.z.ai/api/paas/v4` (insufficient balance 1113) | `https://api.z.ai/api/coding/paas/v4` | The `GLM_API_KEY` is a Coding Plan subscription key. `GLM_BASE_URL=https://api.z.ai/api/coding/paas/v4` already declared the intent — registry was inconsistent. |
| `arliai` | not in registry | added `ProviderConfig("arliai", "bearer", "https://api.arliai.com", "/v1/chat/completions", auth_env="ARLIAI_API_KEY")` | Key existed, base URL existed, but no `ProviderConfig` mapped it. |

### Verified working

| Provider | Key | Endpoint | Result |
|----------|-----|----------|--------|
| `openrouter` | `sk-or-v1-…` | `https://openrouter.ai/api/v1` | OK on real chat completion |
| `groq` | (empty) | — | no key, no change |
| `google` | `AIzaSy…` | `https://generativelanguage.googleapis.com/v1beta/openai` | OK |
| `minimax` | `sk-cp-w-…` (Coding Plan) | `https://api.minimax.io/v1` | OK |
| `synthetic` | `syn_…` | `https://api.synthetic.new/openai/v1` | OK |
| `ollama-cloud` | `ec71c…` | `https://ollama.com/v1` | OK |
| `opencode-zen` | `sk-bPJlee…` | `https://opencode.ai/zen/v1` | OK; key is on free tier — only `*-free` and `big-pickle`/`muse-spark-1.2` accessible |
| `opencode-go` | `sk-bPJlee…` | `https://opencode.ai/zen/go/v1` | OK; wider model list (glm-5.3, kimi-k2.6, deepseek-v4-flash, etc.) |

### Upstream issues (not gateway bugs)

| Provider | Symptom | Cause | Action |
|----------|---------|-------|--------|
| `cerebras` | 402 payment_required on `gpt-oss-120b` and `gemma-4-31b` | account not on paid plan | operator action: upgrade Cerebras plan |
| `cohere` | 429 trial-key 1000/month limit | key is on free trial | operator action: provide paid key |
| `xiaomi` | reasoning-only output at low `max_tokens` | `mimo-v2.5` is a reasoning model; reasoning_content fills the budget before content | caller sets higher `max_tokens` |

### Probe notes

- Python `urllib.request` is flagged by Cloudflare as a bot (HTTP 403 error
  code 1010). All probes must use `curl` or a real-browser User-Agent.
  Cerebras and OpenCode both returned 403 to the initial Python probe but
  work via curl with default UA.

## Verification

Smoke test (`/tmp/smoke_final.sh`) — `X-Tusker-Cache: bypass` to skip cache:

```
Xiaomi MiMo (xiaomi::mimo-v2.5-pro)        OK content='OK'
Z.ai Coding   (zai::glm-5.2)                OK content='OK' (model upgraded to glm-5.3)
ARLIAI        (arliai::Fastest)             OK content='OK' (routed to Qwen3.5-27B)
OpenRouter    (openrouter/openai/gpt-4o-mini) OK content='OK'
MiniMax       (minimax/MiniMax-Text-01)     OK content='OK'
Synthetic     (synthetic/hf:zai-org/GLM-5.2) OK content='OK'
Ollama Cloud  (ollama-cloud/deepseek-v4-flash:0731) OK content='OK'
OpenCode Go   (opencode-go/glm-5.3)         OK (reasoning model, content empty at default max_tokens)
Google Gemini (google/gemini-2.5-flash)     OK content='OK'

Cerebras      402 payment_required          (upstream billing)
Cohere        429 trial quota               (upstream billing)
OpenCode Zen  500 internal                  (upstream model error on muse-spark-1.2)
```

## Files changed

- `tusker_gateway/config.py` — three `ProviderConfig` updates:
  - `xiaomi`: `https://api.xiaomimimo.com` → `https://token-plan-sgp.xiaomimimo.com`
  - `zai`: `https://api.z.ai/api/paas` → `https://api.z.ai/api/coding/paas`
  - `arliai`: added `ProviderConfig("arliai", "bearer", "https://api.arliai.com", "/v1/chat/completions", auth_env="ARLIAI_API_KEY")`
- `tusker_gateway/config.py` — added `"arliai": ["ARLIAI_API_KEY"]` to `__ENV_KEY_ALIASES`
- `tests/test_passthrough_providers.py` — added `zai::glm-5.2` and `arliai::Fastest` to routing parametrize
