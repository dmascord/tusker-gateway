## 2026-11-23 — Worker pods and Kilo code models: end-to-end verification

### opencode-cli worker: working end-to-end ✅

- Pod `tusker-opencode-worker-5b748999d7-jhrl6`, Running 36h;
  `GET /health` → `{"status": "ok", "mode": "worker"}`.
- Gateway env `TUSKER_OPENCODE_WORKER_URL` points at
  `http://tusker-opencode-worker.hermes.svc.cluster.local:8643`; adapter registry
  contains `opencode-cli → RemoteCLIAdapter`.
- Direct `POST /worker/chat` (`opencode-cli/big-pickle`, non-stream) → HTTP 200,
  `content: "OK"`.
- Through the gateway (`model: opencode-cli/big-pickle`) → HTTP 200,
  `content: "GATEWAY_OK"`, `selected_adapter=RemoteCLIAdapter`.

So the delegation path is functional. Earlier failures were model-specific or
upstream, not the worker:
- `req_0a173b0c24bc76d7`: `opencode-cli/flint-40b-free` hit the 45s per-attempt
  timeout.
- `req_3e2238b844cc6df3`: `claude-code-cli/sonnet` returned worker 502
  `claude_code_cli_quota` (upstream usage limit).
- A `ClientConnectionResetError: Cannot write to closing transport` in the
  worker log is the known client-disconnect case, not a worker fault.

Worker image digest `sha256:800cd56f…`; gateway `sha256:4cc3518…` — separate
deployment pinned to an older build, which is fine for worker-specific code.

### Kilo: working, but NOT via the worker ⚠️

The `tusker-kilo-worker-567d4b9849-phqn6` pod is **orphaned/stale**:
- It runs `python -m tusker_gateway.kilo_worker` from image `sha256:864d057…`.
- `tusker_gateway/kilo_worker.py` was deleted in commit `a874147 "remove Kilo CLI
  worker"` and does not exist in the current source tree.
- No `/health` endpoint on :8643; logs show stale-mode failures:
  `Model not found: openrouter/nvidia/... (Did you mean: nvidia/...)` and
  `permission requested: bash … auto-rejecting`.

The gateway does **not** use it. Registered adapters are only `claude-code-cli`
and `opencode-cli`. `kilo` is a direct HTTP provider:
`https://api.kilo.ai/api/openrouter`, kind `local`, no API key configured,
`selected_adapter=http`.

Kilo code models work end-to-end over that HTTP path:
- real OMP traffic `req_f3f452099d709e9c`: `kilo/particle-ai/particle-1:free`,
  pool `code` → HTTP 200.
- direct checks `kilo/kilo-auto/free` → HTTP 200 (routed to
  `nvidia/nemotron-3-ultra-550b-a55b:free`), `kilo/openrouter/free` → HTTP 200
  (routed to `nvidia/nemotron-3.5-lightning:free`).

### Recommendation

The stale kilo worker deployment is unused and runs removed code from an old
image. It can be removed to avoid confusion and free resources — but that is a
destructive action requiring operator confirmation. No changes were made here.
