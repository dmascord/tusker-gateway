# Privacy pool: Codex seat collapse + uncorrelated capacity failures (2026-10-31)

Two defects surfaced from the privacy-pool report on 2026-10-31 (client saw
"Upstream provider rate limit exceeded; retry shortly." / "Upstream provider
capacity is unavailable; retry shortly."):

1. **The three Codex seats collapsed into one credential.** The rotator's
   store-adoption path matched credentials on `account_id`, which identifies the
   ChatGPT *team workspace*, not the seat. Seat 1 held the only unexpired
   token, so seats 2 and 3 were overwritten with seat 1's rotated tokens and
   the pool held three copies of one access token. One quota 429 then cooled
   all three by token fingerprint and the pool reported
   `pool_empty_or_all_cooling` for `openai-codex/gpt-5.6-luna`.
2. **The capacity SSE frame carried no request ID.** `_public_stream_error`
   appended `Request ID: <rid>.` to its rate-limit arm but not to the
   sibling capacity arm, so a committed HTTP 200 stream terminated with a bare
   "Upstream provider capacity is unavailable; retry shortly." Request
   `req_ef7626de7c191781` (2026-10-31T22:59:12Z) reached the client that way.

## Root cause

### 1. Seat identity keyed on the workspace

`CodexTokenRotator._matches_identity` compared `account_id` first, so any two
credentials from the same workspace were "the same account". Two paths
depended on it:

- `_maybe_adopt_from_store` copies the first matching *stored* credential into
  the in-memory seat. The match was per-workspace, so the first unexpired
  seat's rotated tokens were adopted into every other seat.
- `_credential_identity` produced the key for per-seat model entitlement
  exclusions. Keyed on the workspace, one seat's entitlement failure excluded
  the model for the whole team.

The enrollment tooling already knew this: `enroll_codex_credential` dedupes on
`account_user_id` precisely because team members share `account_id`. The
rotator did not.

### 2. Capacity arm of the public stream error

`_public_stream_error` gained a provider-capacity arm later than the
rate-limit arm; only the latter was updated when request IDs were added to
terminal failures. The JSON paths (`_provider_rate_limit_response`, the
capacity 503, the 502 `provider_error` branch) already carried the ID.

## Fixes

Source:

- `tusker_gateway/passthrough.py`:
  - New `_normalized_identity_value` normalises a claim for comparison
    (whitespace-collapsed, case-folded).
  - `CodexTokenRotator._SEAT_IDENTITY_FIELDS` lists the seat-level claims
    (`account_user_id`, `user_id`, `email`, `label`).
  - `_matches_identity` compares the most specific field both records carry and
    falls back to `account_id` only when no seat-level field is comparable on
    both sides - the shape of a pool enrolled before seat claims were
    recorded. Legacy pools keep adopting as before.
  - `_credential_identity` uses the same order, so an exclusion recorded
    against one seat no longer excludes the model for its team mates.
- `tusker_gateway/endpoints.py`: the capacity arm of `_public_stream_error`
  appends `Request ID: <rid>.`.

Tests:

- `tests/test_codex_oauth.py::test_rotator_keeps_team_seats_distinct_during_store_adoption`
  - three seats of one workspace keep three distinct tokens through store
  adoption.
- `tests/test_codex_oauth.py::test_rotator_model_exclusion_is_scoped_to_one_team_seat`
  - an entitlement failure on one seat leaves the sibling seat serving.
- `tests/test_codex_oauth.py::test_rotator_adopts_fresher_credential_from_store_before_refresh`
  - the pre-seat-claims pool shape still adopts.
- `tests/test_chat.py::test_public_stream_error_correlates_capacity_failure`
  - the capacity SSE frame names the gateway request.

Docs: `docs/gateway-model-routing.md`, "Codex seat identity".

## Residual risk

- The persisted exclusion table (`credential_model_exclusions`) is keyed by the
  hashed identity. The hash changed, so rows written under the old team-wide
  key never match again. The table is not pruned, so those rows remain inert.
- Seats 2 and 3 of the affected pool still need their own refresh tokens to be
  valid. The rotator now refreshes each seat independently and skips a seat
  whose refresh fails instead of collapsing the pool; a revoked refresh token
  still fails that seat alone.
- `github-copilot-enterprise` remains in a 3600s quota cooldown (see
  `TODO.md`). That is an account condition, not a gateway defect.
