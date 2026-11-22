## 2026-11-22 — Getting synthetic routes into privacy image+tools routing

Follow-up to `2026-11-22-privacy-probe-starvation.md`. The privacy pool's
`synthetic` provider had 8 routes, 10 passing `privacy_zdr` records, and still
contributed **zero** candidates for image+tools requests. This record explains
the four gates involved and what was changed.

## Symptom and diagnosis

A synthetic route needs four independent pieces of evidence before pool
selection will admit an image+tools request:

1. `privacy_zdr` passed — most synthetic routes already had this.
2. `input_image` passed — the modality gate. Synthetic's `/v1/models` advertises
   **no input modalities**, so an explicit probe is required; `advertised` or
   `unavailable` records do not admit a model.
3. `input_image_tools` passed — the composite probe.
4. A passing `tool_capability` record — the privacy pool sets
   `require_tool_qualification: true`, so a missing/failed behavioral tool
   qualification excludes auto-discovered routes even when 1–3 pass.

### Root cause of weak composite evidence

`probe_image_tool_model` hardcoded `max_tokens: 2048`. Reasoning models spend
that budget on chain-of-thought before emitting the required tool call, so the
probe recorded false `unavailable` / `unsupported` verdicts. Live evidence:

- `synthetic/hf:Qwen/Qwen3.8-27B`, req_918ddeb1f2b61523: at 2048 the stream had
  `reasoning_chars=9004`, 748 frames, 322 KB, ended
  `UnusableToolResponseError: output_budget_exhausted`. At 16384: `passed`,
  one valid tool call, `finish_reason=tool_calls`.
- `synthetic/hf:zai-org/GLM-5.3-Flash`: `unsupported` at 2048, `passed` at 8192.

Periodic probe latency also exceeded the configured 45s timeout (65–97s), so a
pass would be re-flipped to `unavailable` on the next cycle.

## Fixes

- `tusker_gateway/tool_qualification.py`: `_composite_probe_max_tokens()` reads
  `TUSKER_COMPOSITE_QUALIFICATION_MAX_TOKENS` (default 16384, min 1024); the
  probe payload uses it instead of a literal 2048.
- `tests/test_tool_capability.py`: updated the existing assertion to 16384 and
  added a test for the env override / invalid / clamped values.
- `k8s/deployment.yaml`:
  - `TUSKER_COMPOSITE_QUALIFICATION_MAX_TOKENS=16384`
  - `TUSKER_COMPOSITE_QUALIFICATION_TIMEOUT_SECS` 45 → 120

## Evidence work performed (live)

Re-ran probes against the deployed image with the new cap and 120–150s client
timeouts, then verified all four gates:

| Route | privacy_zdr | input_image | composite | tool_capability |
|---|---|---|---|---|
| `synthetic/hf:Qwen/Qwen3.8-27B` | passed | passed | passed | strict_structured_stream |
| `synthetic/hf:zai-org/GLM-5.3-Flash` | passed | passed | passed | strict_structured_stream |

Modality probes were run with `--ignore-cooldowns --include-unadvertised` —
the ordinary CLI returned `tested=0` because the composite probes had put the
routes in cooldown.

Result (fresh `PoolManager` with catalog refresh, `allowed_providers=("synthetic",)`):

```
plain                 -> synthetic/hf:openai/gpt-oss-120b
tools+image           -> synthetic/hf:zai-org/GLM-5.3-Flash
tools+image+212K      -> synthetic/hf:zai-org/GLM-5.3-Flash
```

Synthetic is now in the image+tools privacy mix. Live traffic is not yet
routing there because ranking prefers higher-ranked providers (apim,
openai-codex, mlx-mac); synthetic serves as additional failover depth.

## Caveats

- **Probe flakiness:** results for these two models flip between runs — Qwen
  passed at 16384 twice but failed once at 8192; GLM-5.3-Flash passed at 8192
  twice but timed out at 16384/90s. A single definitive `unsupported` can
  overwrite a previously passing record. A durable improvement would require
  two consecutive definitive failures before downgrading a passing route.
- `synthetic/hf:moonshotai/Kimi-K3.1-Pro` passes image modality but still fails
  the composite probe with two tool calls / mismatched arguments — a genuine
  strict-contract failure.
- `synthetic/infinite:small:vision` returns upstream **402 "Infinite models
  require an active plan subscription"** — needs a paid Synthetic plan.
- `gpt-oss-120b`, `GLM-4.7-Flash` and `GLM-5.3` return 502 upstream errors on
  image probes and remain text-only.

## Verification

- Deployed source `1ca806fc772f5c1ac11b9d82e43cee438ac22069`, image
  `sha256:4cc351801c10bf8d1821d0fee90907cfcd64dde49d99e8be3e4a83a6d91a0b6f`;
  `k8s/verify-provenance.sh` OK, smoke 200/200.
- Full offline suite: 2208 passed, 8 skipped.
