# Privacy pool: composite evidence starvation (2026-11-01)

Follow-up to `2026-11-01-privacy-pool-codex-seat-collapse.md`. That report fixed
the correlation and seat-identity defects; this one explains why the privacy
pool stayed one candidate deep afterwards, so a single provider 429 still
emptied it and the client read "Upstream provider rate limit exceeded; retry
in about Ns.".

## Symptom

`image` + `tools` requests to the `privacy` pool selected one candidate
(`apim/gpt-5.6-luna`). When that route returned 429 the pool had no fallback,
`no eligible candidates` was logged with `filters=...image_tools=29...`, and
the request failed.

The filter count was the tell: 29 of 31 routes were rejected for missing
image-plus-tool evidence. Only the two curated static routes could ever be
selected, and one of them (`mlx-mac/qwen3.8-27b`) is `unsupported` for this
contract, so the pool was effectively one route.

## Root cause

Two independent defects kept composite evidence from ever being collected for
the auto-discovered routes.

### 1. The first run waited a full interval

`qualification_maintenance_loop` set

```python
composite_next_due_at = time.monotonic() + composite_interval_secs
```

so the first composite probe was due 12 hours after startup. Pods are replaced
well inside that window — six ReplicaSet revisions were created on 2026-10-31
and 2026-11-01 alone, and the running pod was 1.33h old against a 12h interval.
A pod that never reaches the interval never produces a record. The modality
probe already had the correct shape (`initial_delay + interval`); composite did
not.

### 2. A bounded batch starved every other provider

`run_image_tool_qualification` trimmed its candidate list with

```python
pairs = pairs[: max(0, limit)]
```

after `sorted(pairs)`. `unavailable` is deliberately always retried (a transient
429 must not become a permanent denial), and because sort order is provider name
first, the eight `github-copilot-enterprise` routes — all `unavailable` from a
3600s account quota cooldown — were the first eight every single cycle. They
could never drain, so the twelve `ollama-cloud` routes, which had no composite
record at all, were never examined.

Measured on the live pool (31 routes, 29 due):

|Ordering|Batch of 8|Providers covered|`ollama-cloud` routes probed|
|---|---|---|---|
|`sorted[:8]` (old)|8 × `github-copilot-enterprise`|1|0|
|interleaved (new)|3 × `ollama-cloud`, 2 × `openai-codex`, 2 × `github-copilot-enterprise`, 1 × `mlx-mac`|4|3|

## Fixes

Source:

- `tusker_gateway/maintenance.py`
  - `_composite_qualification_settings` returns a sixth value,
    `TUSKER_COMPOSITE_QUALIFICATION_INITIAL_DELAY_SECS` (default 600s).
  - `composite_next_due_at` is scheduled off that delay instead of
    `composite_interval_secs`.
- `tusker_gateway/tool_qualification.py`
  - New `_interleave_probe_candidates` orders the due set: routes with no
    record, or with `passed`/`unsupported` evidence, first; then the rest
    round-robin across providers so a bounded batch still spans the pool.
- `k8s/deployment.yaml`: `TUSKER_COMPOSITE_QUALIFICATION_INITIAL_DELAY_SECS=600`
  declared explicitly.

Tests:

- `tests/test_maintenance.py::test_composite_probe_runs_on_initial_delay_not_full_interval`
  - the scheduler fires the first composite cycle on the initial delay.
- `tests/test_tool_capability.py::test_composite_probe_batch_does_not_starve_a_provider`
  - unproven routes take the batch instead of a cooled provider.
- `tests/test_tool_capability.py::test_composite_probe_batch_spans_providers_when_all_are_unproven`
  - an all-unproven set still samples every provider.

All three fail against the pre-fix source.

Docs: `docs/gateway-model-routing.md`, "Composite probe scheduling and batch
composition".

## Residual risk

- `github-copilot-enterprise` remains in a 3600s quota cooldown. That is an
  account condition; the fix only stops it consuming the whole probe budget.
- `mlx-mac/qwen3.8-27b` is `unsupported` for the image-plus-tool contract
  (probe verdict `no_tool_call`). It stays out of image+tools routing until the
  runtime advertises tool support; the composite gate is behaving correctly.
- A provider whose catalog entries are all genuinely unsupported will keep
  retrying `unavailable` routes, though now at a bounded share of each batch
  rather than all of it.
