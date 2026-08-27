# Agent Quality Scoring

`QualityDB`'s original score is transport health: HTTP success rate plus a
latency bonus. It cannot determine whether an OMP agent completed a requested
repository or deployment change.

The evidence scorer is available as:

```bash
python -m tusker_gateway.omp_scoring /path/to/session.jsonl --json
python -m tusker_gateway.omp_scoring /path/to/session.jsonl --last-user-turn --json
```

It reports separate dimensions for:

- protocol health, including provider-error turns;
- tool execution, using explicit OMP `toolResult.isError` values, missing
  results as failures, and unmatched results as uncredited; and
- final-answer verbosity, using only final `text` blocks and ignoring thinking
  blocks.

The observed score is not treated as proof of completion. The default task
state is `unverified`, which produces `hold`. Pass `--task-status failed` only
after an external verifier establishes that the requested outcome was not
achieved; this produces an effective score of zero and `reject`. Pass
`--task-status passed` only after the verifier checks the relevant repository,
tests, secret/configuration state, or live deployment state.

The OMP history can prove that a tool call was emitted, that OMP received a
tool result, and whether that result reported an error. It cannot prove that a
command changed the intended external state without such a verifier.
Use `--last-user-turn` when a long-lived OMP session contains multiple unrelated
tasks; otherwise the report intentionally covers the complete session.
