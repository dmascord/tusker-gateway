## 2026-11-23 — Kilo model qualification and code-pool coverage

Follow-up to `2026-11-23-worker-pod-verification.md`. Verified that Kilo models
appear in the `code` pool and re-ran their qualification probes.

## Catalog vs pool

Kilo's catalog publishes **17** models. **15** appear in the `code` pool, all
auto-discovered. The two absent are:
- `google/lyria-3-pro-preview`
- `google/lyria-3-clip-preview`

Both have `architecture.output_modalities: ["text","audio"]` — they are music/
audio generators, not chat backends, and are correctly excluded by the catalog
output-modality filter. Nothing to fix there.

## Qualification results

All 15 pool routes were re-probed with `--ignore-cooldowns --force`:

| Capability | Passing | Notes |
|---|---:|---|
| behavioral `tool_capability` | **10 / 15** | up from the previous snapshot |
| `input_image` | 4 / 15 | only needed for image-bearing requests |
| `input_image_tools` (composite) | 3 / 15 | rest returned provider/stream errors |

Deterministic tool-qualification failures (same failure class on two runs):
- `humansand/ampersand-v0.1` — stream_error
- `recursive/flint-v1.0` — stream_error
- `inclusionai/ling-3.2-flash:free` — rate_limited
- `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` — stream_validation_error
- `stealth/glyph-cluster` — provider_error

These are genuine upstream faults, not probe artifacts. Direct gateway checks
confirmed: `kilo/recursive/flint-v1.0` → 502 "provider rejected the gateway
credential", `kilo/stealth/glyph-cluster` → 502 provider error. Forcing them
into qualification would route real traffic to broken upstreams.

Composite probes similarly failed for 11/15 with fast `provider_error` /
`stream_error` responses; only `openrouter/free`, `particle-ai/particle-1:free`
and `nvidia/nemotron-3.5-super-vl:free` have all three image+tools gates.

## Selectability after re-qualification

```
plain        15 candidates (all 15 chat models)
tools        10 candidates
tools+image   3 candidates: particle-ai/particle-1:free,
                            openrouter/free,
                            nvidia/nemotron-3.5-super-vl:free
```

Real OMP traffic on a Kilo route was already confirmed in
`2026-11-23-worker-pod-verification.md` (`req_f3f452099d709e9c`, HTTP 200).

## Conclusion

All eligible Kilo chat models already appear in the `code` pool — no config
change was needed. The qualification pass raised tool-qualified routes to 10;
the remaining 5 are genuinely broken upstream and should stay held back from
tool-bearing traffic. No code or config changes were made.
