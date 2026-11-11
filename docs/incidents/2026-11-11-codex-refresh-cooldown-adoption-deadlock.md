# Codex OAuth: refresh-failure cooldown blocked store adoption

**Date:** 2026-11-11
**Impact:** the entire `openai-codex` credential pool reported
`pool_empty_or_all_cooling`, so every privacy-pool request that reached Codex
fell through to weaker candidates. Clients saw
`Upstream provider capacity is unavailable; retry shortly.` (req_670cb08d95d0f14e)
and `Upstream provider error: the upstream rejected the request ...`
(req_90dd52c0b6a7b48c). Both were downstream symptoms, not the cause.

## Root cause

`CodexTokenRotator` recovers a rotated credential in two ways: it refreshes
under its own authority, or it adopts a credential another process already
rotated into the config store. The per-process refresh-failure cooldown
(`_refresh_failed_until`, up to 24h after a permanent OAuth error) was applied
to **both** paths:

1. `has_available_credential()` skipped any expired credential whose cooldown
   was still active, so the request path raised `oauth_credential_unavailable`
   before `get_token()` could ever read the store.
2. `get_token()` ran `_maybe_adopt_from_store()` only when `retry_at <= now`,
   so the store was never read during the cooldown.
3. The canary branch skipped an expired seat without reading the store at all,
   and an adopted refresh token was never added to the canary skip set.

The cooldown is per process and exists to stop replaying a consumed single-use
refresh token. It was never meant to forbid reading the durable store. A pod
that lost the rotation race therefore stayed dead for 24h while the database
held a live credential for the same seat.

## Fix (`tusker_gateway/passthrough.py`)

- Adoption runs for every near-expiry entry **before** any skip policy;
  `_refresh_failed_until` now gates only the OAuth round-trip.
- `has_available_credential()` admits an expired entry while a store is wired,
  because adoption can still recover it.
- Canary: expired seats adopt from the store; an adopted refresh token joins
  `_initial_refresh_tokens` so the canary never rotates a token the other
  holder still owns (also survives `reload()` re-baselining).
- Responses path: if header build yields no `Authorization`, fail as an empty
  pool instead of spending an attempt on a guaranteed upstream 401.
- `POST /admin/codex/credentials/{label}/reset-cooldown` also clears the
  refresh-failure cooldown; previously an operator re-enrol fixed nothing for
  up to 24h.

## Verification

Regression tests in `tests/test_codex_oauth.py` and
`tests/test_passthrough_providers.py`:

- `test_rotator_adopts_store_credential_while_refresh_cooldown_active`
- `test_has_available_credential_allows_adoption_during_refresh_cooldown`
- `test_canary_rotator_adopts_expired_seat_from_store`
- `test_canary_never_rotates_a_token_it_adopted`
- `test_admin_reset_clears_refresh_failure_cooldown`
- `test_codex_expired_seat_with_store_fails_before_sending_request`

A/B on the same in-cluster probe (expired in-memory copies, live store, 24h
cooldown pre-armed): unpatched module returned `has_available=False,
get_token=None`; patched module returned `True/True` via
`oauth credential adopted from store`. After deploying `6e0aaaa` the live pod
selected all three seats and logged per-seat
`codex quota 429 ... cooldown=406941s / 2080s / 2125s`, i.e. the upstream
quota windows are now what limits the pool, not the local cooldown.

## Operational note

At the time of the fix all three Codex seats were genuinely quota-exhausted
upstream (`usage_limit_reached`, plan `team`, `limit_window_minutes=10080`):
codex-01 until 2026-11-15T23:03Z, codex-02 06:35Z and codex-03 06:36Z on
2026-11-11. Rotated refresh tokens are persisted in
`tusker_config_oauth_credentials`; `auth.json` is not the authority.