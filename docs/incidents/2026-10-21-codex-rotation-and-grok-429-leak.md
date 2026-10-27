# Codex refresh-token rotation + Grok/xAI 429 envelope leak (2026-10-21)

Two intertwined outages surfaced from `req_50a994b481aa7fe3` and the
`code` pool fallback logs in `tusker-gateway-689b74674c-kv6qt` around
09:11–09:30Z:

1. **Codex refresh tokens permanently revoked** — every
   `refresh_token_reused`, so the `code` pool's only token-flavoured
   candidate (`gpt-5.6-luna`) was failing closed with
   `503 no usable OAuth credential`.
2. **`_public_stream_error` leaked a Grok/xAI `{"statusCode":429,...}`
   envelope verbatim to the client** because the suppression heuristic
   only matched the OpenAI `{"error":{...}}` shape.

The user-visible symptom was a clean 502 carrying the upstream JSON as
text:

> `Upstream provider error: { "statusCode": 429, "message": "Token limit
> is exceeded. Try again in 8 seconds." }. Request ID: req_50a994b481aa7fe3.`

Source-of-truth files:

- `tusker_gateway/endpoints.py` — `_client_safe_failure_detail`
  hardened to detect non-OpenAI JSON envelopes; `_public_stream_error`
  unchanged.
- `tusker_gateway/cooldown.py` —
  `_cooldown_seconds_for_429` now floors short TPM-flavoured cooldowns
  to 60s via the new `_SHORT_COOLDOWN_FLOOR_HINTS` list.
- Tests: `tests/test_chat.py::test_public_stream_error_suppresses_xai_grok_json_envelope_on_message`
  (and `_on_upstream_body`, `_keeps_plain_text_messages`),
  `tests/test_cooldown.py::test_429_short_stated_cooldown_floored_*`.
- Two **re-pinned** tests in
  `tests/test_reliability_hardening.py`
  (`test_midstream_statuscode_429_envelope_raises_rate_limit_error`,
  `test_midstream_toplevel_statuscode_429_raises_rate_limit_error`)
  that previously asserted the 8.0 cooldown was preserved.

## Root cause

### 1. Codex pool exhaustion

- `tusker_config_oauth_credentials.openai-codex` in the state DB holds
  three rows (`damien.01`, `damien.02`, `damien.03`); all three
  `refresh_token` fields have been **revoked at OpenAI** (every
  `refresh_codex_token` call returns HTTP 401 with
  `error.code = refresh_token_reused`).
- `~/.hermes/auth.json` on visor is from
  `2026-10-14T00:07:59Z` and contains **three duplicate rows of
  `damien.01`** (the other two accounts were lost when the file was
  re-imported). The legacy file-fallback in `app.py:243-280` therefore
  loads three copies of the same dead account into the rotator.
- `_rebuild_rotators` runs on `ConfigRuntime.initialize()` (see
  `tusker_gateway/config_runtime.py:222-260`) and adopts the **DB**
  rows for the live pool, so the in-pod rotator sees the three distinct
  accounts — but they are all `refresh_token_reused`, so
  `get_token()` returns `None` and the codex route fails closed.
- `secrets_authoritative` is **off** in production
  (`config_db_credentials_authoritative` is unset), which means the
  pod startup order is:
  1. Build file-fallback rotator (3 duplicates) →
     `_log_pool_initialized(...)` line.
  2. `ConfigRuntime.initialize()` calls
     `store.runtime_config(old_cfg)` → 3 distinct DB accounts →
     `_rebuild_rotators()` swaps the rotator via `rot.reload(clean)`.

### 2. `_client_safe_failure_detail` whitelist gap

The suppression at `tusker_gateway/endpoints.py:4357-4382` checked
only the OpenAI `{"error":{...}}` shape:

```python
if _RAW_JSON_BODY_RE.match(text) or '"error"' in text:
    return ""
```

xAI/Grok returns the same shape but with the status under `statusCode`
(no `error` key inside the JSON), and depending on the surface it
arrives on `exc.message` (mid-stream) or `exc.upstream_body`. The
OpenAI-only shape check let both through, and the rendered SSE frame
became:

```
data: {"error":{"message":"Upstream provider error:
  { \"statusCode\": 429, \"message\": \"Token limit is exceeded.\n
  Try again in 8 seconds.\" }. Request ID: req_50a994b481aa7fe3."
,"type":"provider_error"}}
```

### 3. `_cooldown_seconds_for_429` floor

The body parses cleanly to `try again in 8 seconds` →
`_cooldown_seconds_for_429` returned **8 seconds**. The breaker
re-probed inside the same minute, hit the same TPM window, and the
mid-stream `RateLimitError` carried that body straight into the
sanitization gap above. Honouring the stated 8 seconds under TPM/RPM
phrasing was wrong: the actual ledger only resets on the next
minute boundary.

## Fixes shipped this session

### Source

- `tusker_gateway/endpoints.py`:
  - New helper `_looks_like_provider_json_envelope(text)` detects any
    `{...}` body that contains at least one key-shaped field, regardless
    of field names.
  - `_client_safe_failure_detail` runs that helper on both
    `upstream_body` *and* the message path before falling through to
    `_safe_upstream_body` or the safe fallback.
- `tusker_gateway/cooldown.py`:
  - New constant `_SHORT_COOLDOWN_FLOOR_HINTS` lists TPM/RPM/Token
    phrasing.
  - `_cooldown_seconds_for_429` raises the floor from `n` (literal
    stated seconds) to **60s** when the body matches any hint and the
    stated cooldown is under one minute; longer still wins.

### Tests

- New: `tests/test_chat.py`
  - `test_public_stream_error_suppresses_xai_grok_json_envelope_on_message`
  - `test_public_stream_error_suppresses_non_openai_envelope_on_upstream_body`
  - `test_public_stream_error_keeps_plain_text_messages`
- New: `tests/test_cooldown.py`
  - `test_429_short_stated_cooldown_floored_under_tpm_phrasing`
  - `test_429_short_stated_cooldown_floored_for_rate_limit_phrasing`
  - `test_429_short_cooldown_preserved_when_body_lacks_quota_phrasing`
- Updated: `tests/test_reliability_hardening.py`
  - Two tests that previously hard-asserted the 8.0 cooldown now
    assert the 60s floor with a pointer to this incident. The
    classification (`upstream_status == 429`,
    `RateLimitError` raised, `_record_stream_failure` awaited) is
    unchanged.

## Operator runbook (live)

Until the three Codex refresh tokens are replaced, the `code` pool
will fail closed on every chat attempt and OMP will route through
synthetic/minimax. To restore Codex:

### Re-enroll each of the three OpenAI accounts

`enroll_codex_credential` drives the OpenAI device-code flow and writes
the result **straight into `tusker_config_oauth_credentials`**, then
bumps the config generation and calls `_refresh_after_write()`. There is
**no separate `migrate_config_to_db` step** — the tool is the write path.

It must run **inside the pod** (`TUSKER_CONFIG_DATABASE_ENABLED=1`);
outside it `ConfigStore._managed` is False and the tool exits 1.

Run it once per account. `--replace-email` overwrites the existing dead
row in place instead of appending a fourth entry:

```bash
kubectl -n hermes exec -it deploy/tusker-gateway -- \
  python3 -m tusker_gateway.tools.enroll_codex_credential \
    --label "codex-01" --replace-email "<email-of-account-1>"
```

Repeat with `--label codex-02 --replace-email <account-2>` and
`--label codex-03 --replace-email <account-3>`.

The tool prints the current pool, then a `chatgpt.com/device` URL and a
user code:

```
  Open https://chatgpt.com/device and enter code: XXXX-XXXX
  (minted …, valid until …)      # device codes live 15 minutes
```

Open the URL, sign in **as the account being replaced**, and approve.
The tool polls until authorization lands, prints
`Authorization received: email=… account_id=…`, writes the row, and
`ConfigRuntime` hot-reloads (generation N → N+1) within one poll cycle.

| Flag | When |
|---|---|
| `--replace-email <email>` | Overwrite the named existing row (use this). |
| `--slot <n>` | Overwrite by pool index instead of email. |
| `--collapse-account` | Drop other entries sharing the same ChatGPT account. |
| `--dry-run` | Run the device flow without writing — sanity-check if unsure. |

Never pass `--allow-duplicate-account`: a second pool entry for the same
account makes each entry's refresh invalidate the other.

### Verify

```bash
kubectl -n hermes exec deploy/tusker-gateway -- python3 -c "
from tusker_gateway.config import load_config
from tusker_gateway.config_store import ConfigStore
store = ConfigStore(database=None, fallback_config=load_config())
cp = store.runtime_config(load_config()).get('credential_pools', {}).get('openai-codex', [])
for i, c in enumerate(cp):
    print(i, c.get('email'), c.get('expires_at_ms'))
"
```

All three rows should show distinct emails and `expires_at_ms` in the
future.

Then watch the codex route recover:

```bash
ssh visor 'kubectl -n hermes logs -f deploy/tusker-gateway \
  | grep -E "passthrough openai-codex|oauth refresh|oauth credential selected"'
```

### What NOT to do

- Do **not** restart the pod to "clear in-process cooldown state" —
  the DB rows are the source of truth, and a restart loses the
  in-memory cooldown timer without resolving the dead refresh tokens.
- Do **not** re-import `auth.json` until the three accounts are
  re-enrolled; importing the stale file will overwrite the DB rows
  with three duplicates of `damien.01` (which is exactly the state
  before this incident).

## Verification (smoke)

Production smoke (online):

```bash
k8s/smoke_production.sh --model gpt-5.6-luna
```

Expect a `200 OK` once the rotation completes; expect 503
`no usable OAuth credential` until the rotation lands.
