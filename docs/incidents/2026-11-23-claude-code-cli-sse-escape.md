## 2026-11-23 — claude-code-cli TypeError from escaped newlines in worker error frames

### Symptom

Three `claude-code-cli/sonnet` requests failed with
`TypeError: 'str' object cannot be interpreted as an integer`, logged as
`provider SSE source failed ... chunks=1 upstream_bytes=160 frames=0`.
The client saw an opaque `provider_unavailable` banner instead of the real
upstream condition (Claude Code CLI quota exhaustion).

### Root cause

`worker.py`'s stream error path wrote:

```python
await resp.write(f"data: {err}\\n\\n".encode("utf-8"))
await resp.write(b"data: [DONE]\\n\\n")
```

In Python source `\\n` is a literal backslash followed by `n`, not a newline.
The worker therefore emitted a single 160-byte chunk with no frame separators.
The gateway's SSE splitter found zero frames, and the downstream parser raised
a `TypeError` while trying to interpret the malformed buffer.

Introduced in commit `f8141b7` ("make worker stream failures surface as SSE
error frames"). The normal success path writes adapter chunks unchanged, so
only failures were affected.

### Fix

- `worker.py`: use real `\n\n` separators in both error writes.
- `tests/test_worker_error_frame.py`: drives the worker app with a failing
  adapter and asserts the body contains real blank lines, no literal `\\n`,
  and a parseable `data: {"error": ...}` frame followed by `data: [DONE]`.

### Deployed and verified

Gateway and both worker deployments run image
`sha256:b85fda18c7c269e46980ddd99fb730dd496a828e4af64b0c7ba73759eb5bf0ae`
(source `905322f`); provenance OK, smoke 200/200. A live claude-code-cli
stream request now returns four well-formed SSE frames ending in `[DONE]`
with a client-visible "Upstream provider..." message — no TypeError in the
gateway logs.

Full offline suite: 2220 passed, 8 skipped.
