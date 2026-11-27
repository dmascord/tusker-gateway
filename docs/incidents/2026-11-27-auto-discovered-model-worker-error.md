## 2026-11-27 — Auto-discovered model advertised by catalog but rejected at inference

### Symptom

`req_6cf0e5a0d1d0bbd7` failed with:

> Upstream provider error: All 12 eligible provider candidates failed: provider_error x2, worker_error x10. Request ID: req_6cf0e5a0d1d0bbd7.

The aggregate verdict is correct in shape (`ProviderFailoverError`, see
`2026-11-22-failover-terminal-error.md`) but hid the real cause: the requested
`seaglass-alpha-free` is an **auto-discovered** `opencode-cli` route that the
worker could not actually serve.

### Evidence

- The `code` pool opts `opencode-cli` into auto-catalog
  (`tusker_config_pools.auto_catalog_providers`).
- `OpenCodeCLICatalog.fetch()` discovers models by running `opencode models`
  inside the gateway pod; that command lists `opencode/seaglass-alpha-free`
  (and `opencode/tidepool-alpha-free`) in both gateway and worker pods.
- Gateway log: `catalog refresh provider=opencode-cli status=ok entries=11`
  at 04:49:57Z and 04:58:54Z. The model was not in the `excluded N unpriced
  models` line, so it was admitted as a normal auto-discovered candidate.
- Worker log:
  `provider.no-route: Model unavailable: opencode/seaglass-alpha-free` and
  `provider.invalid-request: Upstream request failed: Model is unavailable.`
- `permanent_failures` had **0 rows** for `opencode-cli` / `seaglass`, so the
  dead route was never pruned and re-entered rotation on each refresh.
- All gateway and worker pods were Running/Ready at the same digest; this was
  not OOM or a down worker.

### Root cause

OpenCode's CLI catalog advertises models that its inference endpoint may still
refuse — exactly the case `_mark_permanently_failed()` was written for (its
docstring names catalog-advertised models that 404 at inference). The
classification was lost across the worker boundary:

1. `ProviderModelRetiredError` sets `upstream_status = 410` but leaves the
   inherited client-safe `status = 502`. `worker.py` mapped adapter exceptions
   with `getattr(exc, "status", 502)`, so the worker returned **502**.
2. `remote_cli.py` discarded the worker's
   `{"error": {"message": ..., "code": ...}}` body and raised a generic
   `ProviderError(code="worker_error")`, dropping both `code` and
   `upstream_status`. No permanent-failure mark, no quota cooldown.
3. `_MODEL_REJECTION_RE` in `opencode_cli.py` matched `"model is unavailable"`
   but not the CLI's `"Model unavailable: <slug>"` form.
4. `cooldown.is_model_unavailable_error()` only matched Copilot-specific
   phrases, so a 400-body fallback could not recover it either.

Failover therefore retried a deterministically-dead route until 12 candidates
were exhausted.

### Fix

1. `tusker_gateway/worker.py`: derive the HTTP status from `upstream_status`
   when present, and forward `code`, `upstream_status`, and rate-limit
   `body`/`headers` in the JSON error body.
2. `tusker_gateway/provider_adapters/remote_cli.py`: rebuild the original error
   class from that body (`model_retired`, `provider_auth_failed`,
   `rate_limit_exceeded`), preserving `upstream_status`/`upstream_body` for
   the gateway's cooldown and permanent-failure logic. Unparseable bodies keep
   the generic `worker_error` behaviour.
3. `tusker_gateway/provider_adapters/opencode_cli.py`: widen
   `_MODEL_REJECTION_RE` to `model\s+(?:is\s+)?unavailable`.
4. Regression tests in `tests/test_worker_error_frame.py`,
   `tests/test_remote_cli_errors.py`, and `tests/test_opencode_cli_adapter.py`,
   including an end-to-end assertion that a retired model from the worker
   reaches `_mark_permanently_failed()`.

### Verification

- Focused tests: 62 passed.
- `pytest tests/ -p no:cacheprovider --ignore=tests/test_passthrough_providers.py`:
  2354 passed, 8 skipped.

### Not changed

The upstream catalog still advertises an unservable model. After this fix the
first failure marks `opencode-cli/seaglass-alpha-free` permanently failed and
auto-catalog prunes it on the next refresh, so callers no longer pay for the
dead route.

### Deployment

Deployed from commit `5d2e3ff88706fff95864bf171c4331a1c89769d4`, image
`registry.tusker.net.au:5000/tusker-gateway@sha256:99a07b67b93b30a40324e7c012f45803acd28b9916c05f6bcfdb3a8305465301`
(tag `swarm-alpine-5d2e3ff88706fff95864bf171c4331a1c89769d4`). All three
workloads (`tusker-gateway`, `tusker-opencode-worker`, `tusker-claude-worker`)
rolled out and reported matching imageIDs; `/health` reported the same commit.
`k8s/verify-provenance.sh` passed: tracked manifests, live Deployments, running
pods and `/health` all agree. `k8s/smoke_production.sh` passed after rollout.
