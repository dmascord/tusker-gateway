## 2026-11-16 — redaction tokens reaching OMP sessions

### Symptom

Sealed email tokens of the form `[TUSKER:email:…]` appeared verbatim in
recent OMP session transcripts, including inside assistant `toolCall`
records. The first report named token
`7dFfJ6USK3MCWnaZr81y4MWRODgZ0Zw1RIjK9n0Qp0UE`.

### Reproduction

`ready_to_fly_api/2026-11-15T02-52-52-251Z_01a1dd1f-…jsonl` line 5389
(2026-11-16T08:51:53Z): an assistant `bash` tool call issued
`ssh -p 22 [TUSKER:email:7dFfJ6…]` inside its arguments. The next tool call
against the same host carried a *different* opaque token (`p9M2Cs11…`), which a
deterministic sealer cannot produce for the same value — the model re-typed
the ciphertext and mangled it.

The gateway's restore path is fail-safe by design: a token that fails AES-SIV
authentication passes through verbatim. A mangled re-type therefore surfaces to
the client exactly as the model wrote it.

### Root cause

Production ran with `TUSKER_REDACTION_FORMAT_PRESERVING=false`, so upstream
models saw opaque `[TUSKER:email:…]` ciphertext instead of a syntax-shaped
`u@…redact.invalid` alias. The harness system prompt
(`_HARNESS_GUARDRAIL_SYSTEM_PROMPT` in `tusker_gateway/guardrails.py`)
reinforced the wrong pattern by telling the model to pass `[TUSKER:…]` values
"verbatim to tools".

### Fixes

1. `k8s/deployment.yaml`: `TUSKER_REDACTION_FORMAT_PRESERVING='true'`.
   Upstream models now see `u@<ciphertext>.redact.invalid` aliases, which
   survive a plain copy; the gateway restores them before tool execution.
2. `guardrails.py` prompt reworded: redacted values are described as opaque
   syntax-shaped placeholders to preserve exactly, not as `[TUSKER:…]` tokens to
   retype.
3. Regression tests: `test_deployment_enables_syntax_shaped_redaction_for_omp`
   and the updated `test_guard_injects_system_prompt_for_omp_harness_principal`
   assertions fail against the pre-change source (verified via `git stash`) and
   pass after it.

### Verification

- Offline suite: 2145 passed, 8 skipped
  (`pytest tests/ -p no:cacheprovider --ignore=tests/test_passthrough_providers.py`,
  2026-11-16).
- Deployed: commit `8032f27a12c2bb32069e9ecb787cb009a2814f45`, image
  `registry.tusker.net.au:5000/tusker-gateway@sha256:479cce1bf0308e154ec27e0325c6f987c30f2e0b0dbf97c545f94794977e2e9e`.
  Branch `fix/redaction-token-leak-20261116`, manifest pin commit `bd844b0a`.
- `k8s/verify-provenance.sh` on visor: tracked manifest, live Deployment,
  gateway pod imageID, registry tag, `/health` and the Deployment annotation
  all agree.
- `k8s/smoke_production.sh`: `{"ok": true, "reason": "ok", "http_status": 200}`.
- Runtime config DB re-synced with
  `python -m tusker_gateway.tools.migrate_config_to_db --update`: 53 rows
  updated, generation 1222.
- In-pod check of the live tokenizer: `format_preserving_aliases=True`, alias
  shape `u@<b32>.redact.invalid`, round-trip restore `True`.

### Open items

- The mangled-retype mechanism is inference from the two differing tokens on
  one host in the same session. Reproducing it exactly needs the production
  `TUSKER_REDACTION_KEY`, which is not available locally.
- Older sessions keep historical `[TUSKER:email:…]` records; no cleanup is
  planned because they are private to the operator's own OMP storage.
