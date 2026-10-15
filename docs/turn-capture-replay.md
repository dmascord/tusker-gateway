# Rejected-turn capture and replay

A turn the gateway rejects before the client sees it is re-dispatched, so the
rejected provider's own bytes disappear once the turn resolves. Those bytes (the
request the gateway actually sent and the upstream response frames) plus the
verdict counters are the only evidence that the verdict was correct.

## Why the store moved

Captures used to be `unusable-*.json` files under the `tusker-home` PVC capture
directory. That did not survive to the point where a capture could be triaged:

- Container logs rotate after roughly 20 minutes under production log volume, so
  a log-only record is gone before an investigation starts.
- The on-disk store stopped silently once its budget filled (200 files / 2 MiB)
  and needed an operator to clear the directory before capture resumed.
- A rejection must stay queryable long enough to be triaged and replayed.

Rejected provider turns are now rows in the shared state database (table
`turn_captures`, written by `tusker_gateway/turn_capture.py`). The capture
directory now holds only reasoning-cycle captures (`cycle-*.jsonl`), which keep
their own bounded 200-file / 2 MiB store.

## Retention

- Default TTL is 7 days (`TUSKER_TURN_CAPTURE_TTL_SECS` = 604800). Every row
  carries `created_at` and `expires_at`.
- Expired rows are pruned on write (at most once per 300 s while the store is
  recording); the store also exposes an explicit prune.
- The store uses the shared state database: SQLite in dev, PostgreSQL via
  `TUSKER_STATE_DATABASE_URL` in production, over the same adapter the other
  state stores use.
- Writes take the `advisory` fallback policy: capture is diagnostics, so a
  database outage degrades to "no capture" and never fails a client request.

## Environment knobs

Enable switch is shared with the historical directory capture, so the
deployment flag keeps its meaning.

| Variable | Default | Meaning |
|---|---|---|
| `TUSKER_STREAM_CAPTURE` | (unset = off) | Enable switch ("1"/"true"/"yes"/"on"). |
| `TUSKER_TURN_CAPTURE_PATH` | `/home/tusker/.hermes/turn-captures.db` when that directory exists, else `/tmp/tusker-turn-captures.db` | SQLite path used when the shared state DSN is empty. |
| `TUSKER_TURN_CAPTURE_TTL_SECS` | 604800 | Row lifetime in seconds (7 days). |
| `TUSKER_TURN_CAPTURE_MAX_REQUEST_BYTES` | 4194304 | Request body bound (4 MiB); oversize requests are excerpted head+tail. |
| `TUSKER_TURN_CAPTURE_MAX_RESPONSE_BYTES` | 262144 | Response body bound (256 KiB); oversize responses are excerpted head+tail. |
| `TUSKER_TURN_CAPTURE_PENDING_BYTES` | 16777216 | Bound on request bytes pinned in memory while streaming turns are in flight. |
| `TUSKER_TURN_CAPTURE_EXCLUDE_POOLS` | `privacy` | Comma-separated pools whose prompt bodies are withheld. |

## Privacy policy

Prompt/response bytes are persisted only for pools that are neither excluded by
name nor unknown. Each row records why bytes are present or absent in
`body_policy`, so an auditor can tell a complete capture from a deliberate
omission without reading the payloads.

| `body_policy` | Bytes stored |
|---|---|
| `captured` | Request and response bodies (bounded, excerpted if oversize). |
| `request_unavailable` | Pool not excluded, but the request body was not held (process-backed adapter, or the hold expired/evicted). Response body still stored. |
| `excluded_pool` | No prompt or response bytes; verdict counters only. |
| `unknown_pool` | The pool could not be established, so no bytes; verdict counters only. |

The `privacy` pool is the ZDR boundary and is excluded by default, so prompt
bodies are never persisted for it. A turn whose pool cannot be established keeps
its verdict counters but no bytes, because the ZDR policy cannot be proven for
it.

`replayable` is true only when a byte-complete request body was stored.
Oversize requests are excerpted head+tail with an elision marker, so they record
the original size and sha256 but cannot replay (replaying an excerpt would send a
different request).

## Operations

Find and read captures through the admin API:

```
GET /admin/captures?provider=&model=&pool=&reason=&request_id=&since=&limit=
GET /admin/captures/{capture_id}
```

- The list endpoint returns metadata newest first (`{"captures": [...], "count":
  N}`), with no payload text; `limit` defaults to 50 and is clamped to 1..500;
  `since` is an epoch-seconds float.
- The get-by-id endpoint returns the full record including `request_text` /
  `response_text` and `request_base64` / `response_base64` (bodies rendered for
  JSON transport), or 404 when the id is absent.
- Both sit behind the standard `/admin` access guard.

Replay a capture from the command line:

```
python3 -m tusker_gateway.tools.replay_capture --capture-id CAPTURE_ID
python3 -m tusker_gateway.tools.replay_capture --request-id REQUEST_ID
python3 -m tusker_gateway.tools.replay_capture --list [--provider ... --model ...
    --pool ... --reason ... --request-id ... --since ... --limit ...]
```

- Without `--list`, `--capture-id` selects one capture and `--request-id`
  selects the newest matching capture; those two selectors cannot be combined.
  With `--list`, `--request-id` is a list filter.
- The tool re-issues the stored request bytes verbatim to the provider's
  `base_url` + chat path with the provider credential; it does not send the
  request through the live gateway. So after a replay, post-verify the result
  against the live gateway rather than treating the tool's output as
  end-to-end.
- Payload text is printed only with `--show-body`.
- Exit codes: `0` = the replay looks usable; `3` = the turn reproduces the
  unusable-tool-turn failure (tool markup surfaced as literal text / no usable
  tool calls); `2` = refusal or configuration error (not found, not replayable,
  provider requires oauth/codex or a local adapter, or the credential is
  missing).

## Cleanup

The legacy `unusable-*.json` files in the capture directory are no longer written
or counted. They can be deleted by an operator; deleting them does not affect the
database store or the reasoning-cycle captures.