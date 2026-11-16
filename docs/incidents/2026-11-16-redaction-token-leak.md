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
- The exact leak path (a model mutating an alias into an unopenable token) is
  not reproducible without the production `TUSKER_REDACTION_KEY`, so the
  mangled-retype mechanism remains inference from the differing tokens on one
  host in the same session.

### Open items

- The DB-backed config store shadows env vars for pool/provider config;
  `migrate_config_to_db --update` must run after the deploy so the alias flag
  reaches the DB snapshot. Guardrail flags are read from the environment at
  startup, so the deployment env change is what makes them effective.
- Older sessions keep historical `[TUSKER:email:…]` records; no cleanup is
  planned because they are private to the operator's own OMP storage.
