# Project TODOs


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
