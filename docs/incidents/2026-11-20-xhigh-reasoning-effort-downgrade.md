## 2026-11-20 — `xhigh` reasoning_effort rejected by non-Codex upstreams

### Symptom

A turn that asked for `reasoning_effort: "xhigh"` failed outright on
upstreams that do not implement the Codex-specific level. The upstream
answer was `invalid reasoning value: 'xhigh'`, which rejected the whole
request instead of the reasoning setting.

The level is OpenAI/Codex-specific: models released after GPT-5.1 Codex
Max accept `none, low, medium, high, xhigh`, while earlier models and
third-party hosts reject `xhigh`. In this gateway `xhigh` reached
`mlx-mac`, `apim`, `alibaba` and other backends verbatim, so any client
that asked for it (OMP sends `xhigh` for the strongest reasoning budget)
lost the turn.

### Root cause

`tusker_gateway/passthrough.py` normalized `reasoning_effort` only for
the `opencode-go` provider and only for the `minimal`/`maximal` client
aliases. `xhigh` was passed through to every provider unchanged, so
providers whose backends do not implement the level rejected the request.

The fix normalizes per provider: `xhigh` is downgraded to `high` for
every backend except the two that accept it natively (`openai-codex`,
`opencode-go`), which keep the requested level. The conservative
downgrade is applied when the provider is unknown, so an unlisted
backend is never handed a level it may reject. The other levels
(`low`, `medium`, `high`, `max`) are never rewritten.

The normalization is applied on both request paths: the chat-completions
body (`reasoning_effort`) and the Responses body
(`reasoning.effort`), including a nested `reasoning` object supplied by
the caller through `extra_body`.

### Fixes

1. `tusker_gateway/passthrough.py`: `_normalize_reasoning_effort` takes the
   provider and downgrades `xhigh` to `high` for backends that do not
   accept it; both request paths pass the provider through.
2. `k8s/deployment.yaml`: manifest re-pinned to the image that actually
   runs (see Verification).
3. Regression tests: `tests/test_reasoning_effort_xhigh.py` (18 tests).
   The two request-body tests fail against the pre-change source
   (verified by reverting the normalization) and pass after it.

### Verification

- Deployed: commit `4c9ed1326226b27d6998dbde6ad943c80e91399e`, image
  `registry.tusker.net.au:5000/tusker-gateway@sha256:290416108f4677715693ec06a9946a2fcd563915819cbdb0358cbf8a8dcccf21`,
  tag `swarm-alpine-20261120150414`.
- `k8s/verify-provenance.sh`: tracked manifest, live Deployment, gateway
  pod imageID, registry tag, `/health` and the Deployment annotation all
  agree — `OK: tracked manifest, live Deployment, running pods and
  /health all agree`.
- In-pod check of the running image:
  `xhigh/mlx-mac -> high`, `xhigh/apim -> high`,
  `xhigh/openai-codex -> xhigh`, `xhigh/opencode-go -> xhigh`,
  `high/mlx-mac -> high`.
- `k8s/smoke_production.sh`: `{"ok": true, "reason": "ok",
  "http_status": 200}` for `hermes-privacy`, `hermes-code` and
  `--with-media`.

### Notes

- This deploy landed on top of the `fix/context-estimate-and-redaction-prompt`
  merge (`ce6870b`) and was applied by a concurrent session on the
  `fix/xhigh-reasoning-effort` branch. The manifest pin that shipped with
  it named an image that was already superseded, so the tracked manifest
  briefly pointed at a digest no pod ran. The re-pin in this commit
  corrects the manifest to the digest that is live.
- The `xhigh` downgrade is a compatibility measure, not a capability
  upgrade: providers that support `xhigh` natively but are not
  `openai-codex`/`opencode-go` still receive `high`. Extend
  `_XHIGH_CAPABLE_PROVIDERS` when such a provider is confirmed.
