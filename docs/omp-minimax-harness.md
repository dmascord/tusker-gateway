# OMP/MiniMax comparison harness

`tests/omp_minimax_harness.py` exercises an OMP-like multi-turn tool loop. It
feeds model tool calls synthetic successful results, performs one compaction,
and measures repeated skill reads and streamed tool-call indexes.

The live comparison uses the same request flow against:

1. MiniMax directly at `api.minimax.io` using `MINIMAX_API_KEY` and model
   `MiniMax-M3`.
2. Tusker Gateway at `ai.tusker.net.au` using `GATEWAY_API_KEY` and the pinned
   model `minimax::MiniMax-M3`.

The gateway requests include one stable `x-opencode-session` value across all
turns. No real tool is executed; tool results are synthetic. The harness does
not print response text, tool arguments, request bodies, API keys, or error
bodies.

## Offline replay

```bash
python tests/omp_minimax_harness.py --replay-only
```

This proves the expected difference between a lossy compaction summary and a
summary that retains completed skill-resource state.

## Live comparison

Live calls are opt-in:

```bash
MINIMAX_API_KEY='...' GATEWAY_API_KEY='...' \
  python tests/omp_minimax_harness.py --live
```

Useful controls:

```bash
python tests/omp_minimax_harness.py --live \
  --turns 5 --compaction-after 2 --compaction-mode both
```

The output includes HTTP status counts, repeated skill-read counts, missing or
malformed wire indexes, index collisions, ID/index conflicts, finish reasons,
and per-turn latency. A healthy comparison should have no wire index
collisions or ID/index conflicts; differences in repeated skill reads after
lossy compaction are evidence about harness/model state rather than gateway
stream assembly.
