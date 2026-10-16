# Architecture remediation roadmap

## Purpose

Tusker Gateway has evolved from a lightweight proxy into an AI inference control plane with API compatibility, routing policy, provider adaptation, streaming state, quality feedback, enterprise controls, media services and shared persistence. This roadmap reduces regression risk without undertaking a high-risk rewrite.

## Target subsystem boundaries

1. **Northbound API compatibility**: request validation and OpenAI/Anthropic-compatible contracts.
2. **Routing and policy**: aliases, pools, qualifications, budgets and candidate plans.
3. **Provider adaptation**: provider-specific request/response translation only.
4. **Execution and transport**: HTTP/CLI execution, timeout budgets, cancellation and limits.
5. **Streaming state**: event normalisation, tool assembly, reasoning boundaries and completion.
6. **Quality and feedback**: outcome classification, correlation and ranking updates.
7. **Enterprise governance**: identity, approval, guardrails, audit and administration.
8. **Persistence**: explicit process-local, pod-local and cluster-shared state contracts.
9. **Operations**: health, readiness, metrics, deployment provenance and maintenance.

## Safe extraction sequence

### Phase 1: characterisation

- Freeze growth of the five largest modules with `tools/check_module_size.py`.
- Add state-machine tests before moving streaming or fallback logic.
- Capture p50/p95 test duration and warning counts in CI.
- Document the ownership and consistency level of every persistent store.

### Phase 2: endpoint decomposition

Extract pure logic from `endpoints.py` in this order:

1. tool-call fragment assembly;
2. Responses API feedback correlation;
3. stream completion classification;
4. provider-independent candidate orchestration;
5. thin Chat Completions and Responses handlers.

No extraction should change a public response, error code, fallback rule or access-log field.

### Phase 3: transport decomposition

Split `passthrough.py` into timeout budgeting, request transport, stream reading, cancellation, credential rotation and provider-specific request construction. Retries must consume one shared request deadline and must not occur after client-visible stream output.

### Phase 4: typed application state

Replace string application/request keys with typed `aiohttp` keys through a compatibility layer that still supports the minimum declared `aiohttp` version. Remove warning filters only after the migration is complete.

### Phase 5: stronger gates

Introduce broader Ruff rules, type checking on stable domain modules, branch coverage for routing/streaming state machines, container scanning, manifest validation and secret scanning. Baseline existing findings and reject only new debt during migration.

## Rules for refactoring

- Prefer small extraction-only pull requests.
- Preserve tests before changing implementation.
- Do not combine architectural movement with provider behaviour changes.
- Record state ownership and failure behaviour for each extracted component.
- Reduce hotspot baselines after each successful extraction.
