# OMP todo tool — test scenarios for schema normalization

The gateway uses the OMP harness's `todo` tool to track multi-step work. Across
recent sessions the tool consistently failed to resolve per-item `done`
addresses when the originating `init` payload used a flat `list[].items: [...]`
array. The behaviour reproduced reliably enough to use as a specification for
the harness team's schema normalization tests.

## Observed behaviour

When `init` is called with a phase carrying a list of items:

```json
{
  "op": "init",
  "list": [{
    "phase": "Deploy-and-verify",
    "items": [
      "Push the two commits to origin/main",
      "rsync source to visor:/srv/opencode/tusker-ai-gateway/",
      "Run ./k8s/deploy.sh (buildah build, push, apply, rollout)",
      "..."
    ]
  }]
}
```

…the engine stores the entire list as a single aggregate value rather than
expanding to one task record per string. Subsequent `done` calls reference a
task id that does not exist in the engine's record, and return:

```
Errors: Task "<verbatim item string>" not found
```

The state reported back after the failure still shows the original list as a
single nested-item value, confirming the aggregate-storage hypothesis.

The only way to clear the phase today is to call `done` against an
"aggregate-clear" target (a single summary string, or the original array
verbatim). The `init`-then-`done`-per-item workflow is broken.

## Test scenarios

The harness team should be able to run each scenario as a single transcript
and assert on the resulting state. Each row is independent.

| # | Scenario | Expected behaviour | Acceptance |
|---|----------|-------------------|-----------|
| 1 | `init` with 3 items, then `done(task=items[0])` | task 0 cleared; 1 and 2 still open | state after `done` lists exactly 2 open items, neither equal to items[0] |
| 2 | `init` with 3 items, then `done(task=items[2])` | task 2 cleared; 0 and 1 still open | symmetric to case 1 |
| 3 | `init` with 9 items, then `done` against each item in turn | each `done` resolves; state shrinks monotonically | 9 successful `done` calls; final state empty |
| 4 | `init` with N items, then `done(task=items[0])` twice | second call is a no-op or returns explicit "already done" | no error |
| 5 | `init` with one phase containing 9 items, then `init` again with the same payload | either idempotent (no state change) or explicit "replace existing phase" | clear semantics, not silent re-bloating |
| 6 | `init` with `items: [{item: "..."}]` (nested-object form used in early 2026 sessions) | each `item` string is a first-class task | per-item `done` works |
| 7 | `init` with 3 items, then `done(task=phase_name)` | either marks entire phase done (atomic clear) or returns explicit "use done-all" | not a silent no-op |
| 8 | `init` with `description` plus `items` | description rendered, items still individually addressable | description is metadata, not a task |
| 9 | Items containing emoji and CJK (e.g. "✅ ship it", "デプロイ確認") | preserved as exact-match keys | `done` resolves the same string back |
| 10 | `init` with `items: []` | empty phase accepted and rendered as "(no items)" | no error |
| 11 | `done` with leading or trailing whitespace mismatch (e.g. `"  foo"`) | either exact-match policy stated in error, or fuzzy match | error message is informative, not `not found` |
| 12 | `init` then `append(items=["d"])` then `done(task="d")` | append-target found | `done` resolves against appended items, not just the original `init` list |

## Schema-normalization contract

The cases above imply a single normalization contract the harness should
satisfy:

> A `phase` whose `items` value is a list of strings MUST be expanded into
> one task record per string. Each record carries an opaque id the harness
> can echo back, and a human-readable label equal to the original string.
> A `done` call addressed to a record's id (or its exact label) MUST clear
> only that record.

Two rules fall out:

1. **Identity preservation.** Whitespace, emoji, and non-ASCII characters
   are part of the key. A `done` call must round-trip the original string
   unmodified. Trimming or case-folding silently breaks the address.
2. **No aggregate storage.** Storing the whole `items` array under one
   record and expecting `done` to index into it is the root cause of the
   session failures. The `init` payload is a *factory* for records, not
   the record itself.

## Suggested fixture

```json
// init
{
  "op": "init",
  "list": [{
    "phase": "Phase-A",
    "description": "Workstream description",
    "items": ["alpha", "beta", "gamma"]
  }]
}

// expected engine state (conceptual)
[
  {"id": "phase-a.0", "label": "alpha", "status": "open"},
  {"id": "phase-a.1", "label": "beta",  "status": "open"},
  {"id": "phase-a.2", "label": "gamma", "status": "open"}
]

// done
{ "op": "done", "task": "alpha" }
// or
{ "op": "done", "task": "phase-a.0" }
```

The harness should accept either address form and update exactly the one
matching record.

## Feedback channel

The OMP harness is a closed-source product distributed as a compiled native
(`pi_natives.darwin-arm64.node` in the OMP install path), so this document
cannot be filed as a PR upstream. The intended consumers are:

1. **OMP team members** who have a configured feedback channel inside the
   product. The harness UI may surface a "Report an issue" affordance for
   agent-reported bugs; if it does, attach this document or its path.
2. **Operator-side review of the schema contract.** The two rules in the
   *Schema-normalization contract* section above are the actionable
   specification. They can be implemented and tested entirely from the
   user-visible behaviour in the test scenarios.

This gateway repository cannot accept code changes to the OMP harness; the
test scenarios and fixture here are documentation only, not a runnable
test suite. The `tests/` directory in this repo exercises the gateway
code, not the harness.

## Evidence trail (this session)

The reproduction came from a 9-item deploy-and-verify plan pushed through
the live gateway. All nine per-item `done` calls were rejected with
`Errors: Task "<verbatim string>" not found`. The workaround — calling
`done` against a single summary string and treating the phase as
indivisible — succeeded and let the session conclude, but the underlying
bug is still present for any future agent that follows the documented
`init` then `done`-per-item pattern.
