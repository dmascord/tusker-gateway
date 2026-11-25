# Project TODOs


## Gateway follow-up audit (2026-11-25) — DONE

Eight defects from the post-resilience audit were fixed in PR #92 and
released as `5594394` (image
`sha256:5e9fe801f300d8f457717de2e5e095e700f8cbe0e55cd3c68f1bb1b69bd285c5`):
the logical backup manifest is in the deploy path, the backup CronJob fails
closed, `/v1/messages` honours the direct-route cooldown gate, reload-built
rotators get the cross-process refresh lock, the SQLite advisory lock is
shared, `/v1/provider-health` reports capacity quarantines, retention
pruning has its own scheduler, and the committed Grafana password was
removed and rotated. Evidence:
`docs/incidents/2026-11-25-gateway-followup-audit.md`.

## Circuit-breaker counter atomicity — OPEN (latent)

`CircuitBreaker.record_failure()` / `record_success()` read the row,
compute the new counters in Python and write them back in a second
statement, so two concurrent failures can both read the same value and both
write the same increment: the second failure is lost and a breaker with a
threshold of 3 stays closed. The half-open multi-probe accounting has the
same shape and contradicts the module documentation ("if all admitted
probes succeed, full traffic resumes; if any fails, the breaker reopens").

**Blast radius: latent.** `TUSKER_CIRCUIT_ENABLED=false` in production, so
the breaker is inert today. The fix needs an atomic
`UPDATE ... SET counter = counter + 1` (or a per-route lock) plus a
decision on the probe accounting, which is larger than a patch.

## Postgres backup, retention and gateway HA (2026-11-25) — DONE

All four items were implemented in the PR following
`docs/resilience-performance-assessment.md`. Evidence and original measured
findings remain in that document; the implementation is summarised here.

1. **Backup + alerts.** `k8s/postgres-backup.yaml` adds a nightly 03:17 UTC
   `pg_dump` CronJob to a separate 10 GiB Longhorn PVC, with gzip integrity
   checks, 7-dump retention and a restore procedure in the manifest comments.
   `k8s/prometheus-rules.yaml` adds five alerts: no ready Postgres pod, restart
   storm, volume >80% full, backup stale >26 h, backup never succeeded. No
   Postgres exporter exists, so the rules use kube-state-metrics / kubelet
   volume metrics and the CronJob's own status.
2. **Retention.** `tusker_gateway/retention.py` runs both `TurnCaptureStore.prune()`
   and `AuditLogger.prune()`; the existing maintenance loop calls it on its
   daily cadence via `asyncio.to_thread`. Expired captures are now removed
   even with no new traffic, and the 90-day audit window is applied.
3. **Safe HA.** `tusker_gateway/oauth_refresh_lock.py` derives a stable key per
   credential; `ConfigStore.oauth_refresh_advisory_lock()` holds a bounded
   (30 s) session-level `pg_try_advisory_lock` across adopt → refresh → CAS
   persist. Fail-open with a process-local fallback if coordination is
   unavailable. `k8s/deployment.yaml` now runs `replicas: 2`.
4. **Duplicate estimate removed.** The chat and Responses handlers compute
   one `estimate_prompt_tokens` per request and pass it to the advisory
   headers; no second serialization at response time.

Regression coverage: `tests/test_resilience_followups.py` (14 tests; 6 fail
against the unfixed tree). Offline suite green. Live verification is recorded in
`docs/resilience-performance-assessment.md`.

**Two defects were found during live verification and fixed before the
follow-up release:**

- The advisory lock was a sync `@contextmanager` used with `async with`, so it
  raised `TypeError` and the fail-open path swallowed it — the cross-process
  protection was silently absent. It is now an `@asynccontextmanager`, and
  `test_refresh_lock_guard_works_with_the_real_store_callback` fails against
  the sync version.
- The backup pod could not reach Postgres: the namespace has
  `default-deny-ingress` and `allow-tusker-gateway-postgres` did not list the
  backup pod. The dump hung at 0 bytes until the pod label was added to the
  policy in `k8s/gateway-postgres.yaml`.

## Quality-ranking selection bottleneck (2026-11-20)

Bounded local profiling found `QualityDB.rank()` dominates selection after the
model-capability snapshot cache: with 361 eligible candidates, 40 plain selects
had a 79.07 ms median and 86.48 ms p90. Replacing ranking with an in-memory
constant-score stub reduced the same path to 3.88 ms median and 10.05 ms p90.
The profiler attributed 1.587 s of 1.930 s total across 30 selects to
`QualityDB.rank()`, including 6,000 `get_quality()` and 6,000
`_recent_failure_count()` calls, each opening a database connection and issuing
a query. Production uses PostgreSQL pooling rather than local SQLite, so these
absolute timings are not production latency; the per-candidate query pattern is
still the next measured bottleneck. Candidate fix: add one bulk ranking query
for the selected candidate set, including recent failure counts, then preserve
the existing score and tie-breaking semantics with regression coverage.

## Pool-selection latency and live provenance drift (2026-11-20)

The live `2309478` image lowered the default candidate idle budget from 45s to
15s without a regression test or deploy record. Two real candidates were then
killed at exactly 15s before their first byte: `minimax/MiniMax-M3` on a
199,814-token prompt and Workers AI on a 22,309-token prompt. A direct
MiniMax probe with the same ~200k prompt returned its first stream bytes in
5.1s, so the 15s default was too aggressive rather than evidence of an
upstream outage. Restore the documented 45s default; retain explicit longer
overrides for cold local routes.

The gateway also spent about 1.2s between candidate attempts because every
`PoolManager.select()` re-read and materialized 33,262 model-capability rows
from the shared database. Selection now keeps a 30-second app-scoped snapshot;
qualification writes become visible when the snapshot expires or the manager
is rebuilt. This removes the repeated full-table scan while preserving the
fallback filters.

The running pod serves commit `2309478b0c6972bcbfb2d88ef829317c57ca1d5d`, but
`k8s/deployment.yaml` still pins `4c9ed1326226b27d6998dbde6ad943c80e91399e`.
Merge the live branch and re-pin the manifest before the next deployment; do
not treat the current cluster state as reproducible source provenance.


## Reasoning-only `length` turns (2026-11-07, 2026-11-08)

Fixed, twice. A reasoning-heavy candidate spent the client's whole 120-token output
budget on reasoning and stopped with `finish_reason=length` and no visible
text. The gateway forwarded that dead terminal, and OMP retried the
identical request three times before raising "Length-stop recovery gave up".

The gateway now rejects a `length` terminal that carries no visible output
or tool call, so the client receives an actionable error instead of a
terminal it cannot recover from, and the reasoning-model floor now covers the
DeepSeek V4 family and five other models confirmed in production logs to
stream a reasoning channel.

The same error recurred on 2026-11-08 against `minimax/MiniMax-M3`, which
streams its chain of thought in the OpenAI `content` channel instead of
`reasoning_content`. The gateway saw "content" and concluded the client had
something to show; OMP moved the leaked thinking into a thinking block and
found nothing deliverable. `tusker_gateway/inband_thinking.py` now classifies
the content the client will actually render, using the same tag rules as OMP.


## xiaomi/mimo-v2.5 retirement + github-copilot re-enable (2026-11-16)

`xiaomi/mimo-v2.5` was retired upstream on 2026-10-18 (`400 Unsupported model
mimo-v2.5`); `/v1/models` now lists only `mimo-v2.6-*`, which have been
quota-exhausted upstream since ~Nov 9. The dead static swarm route produced
0% success across ~31k requests in 30 days. Removed from
`k8s/deployment.yaml` `TUSKER_POOL_SWARM` (commit `f3046b4e`) and from the DB
row `tusker_config_pools.swarm`; generation bumped 1166 → 1167 and the live
pod rebuilt its pools with `xiaomi route_pools: {}`. Re-add a xiaomi route
only after the upstream catalog serves a chat model with quota again.

`github-copilot` (business, login `dmascord`) had been fully disabled since
2026-09-13 (`manual-disable-2026-09-13-100pct-failure`) for an invalid
credential; the credential is now valid (Copilot Business seat, quota
unlimited). The `copilot_internal/v2/token` exchange 403s from the cluster
and from the Mac — a GitHub-side anti-scraping rule, not a credential fault —
but the gateway already falls back to the raw OAuth token and the raw token
serves `/chat/completions` and `/responses` 200 (verified in-pod,
claude-sonnet-5, claude-haiku-4.5, gpt-5.6-luna). Re-enabled in
`tusker_config_provider_settings` (enabled=1, disabled_provider=0, cause
cleared); live pod reports 42 configured/selectable candidates after
generation 1167. `github-copilot-enterprise` remains separately disabled
(invalid API key); untouched.
