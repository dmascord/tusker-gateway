# User-Agent + caller-fingerprint response normalization — investigation (2026-10-16)

Read-only survey in support of a planned per-client response-normalization
feature. The user's chosen signals are `User-Agent` + auth-key fingerprint +
first-message fingerprint. No code was modified during this investigation;
every claim is anchored to a `file:line` in the current working tree (line
numbers verified). Scope surveyed: `TODO.md` open items, every response
builder for chat (streaming + non-streaming), responses, Anthropic messages,
embeddings, rerank, images, video, TTS, models, memory, MCP, admin, and
health/ready/status; every client-fingerprint read site (UA, key
fingerprint, session headers, IP/XFF/Cloudflare).

---

## 1. Executive summary

| | Finding |
|---|---|
| **Response surface** | Five independent frame encoders emit client-visible bytes (`sse_frame`, inline `json.dumps` in chat normalizer, `_responses_sse_frame`, Anthropic translator `_sse_frame`, `openai_error`). A normalization layer cannot rely on a single chokepoint; it needs a per-encoder strategy or a deep refactor of chat-SSE assembly first. |
| **Fingerprint surface** | UA is consumed in exactly **one** place today (`question_adapters.detect_adapter`, `question_adapters.py:69-95`) and only for the approval-question envelope. The auth-key fingerprint (`identity.fingerprint_api_key`, 64-hex SHA-256) is the de-facto client_id and already reaches logs, audit, traces, caches, quotas, idempotency, and approvals — but not `/status`, not `/metrics` labels, not upstream headers. |
| **Existing per-client branching** | Three header-driven branches exist today: `X-Tusker-Harness`/`X-Client-Harness`/body `harness` (the explicit trust-hierarchy header) drives the question envelope; `X-Tusker-Tool-Qualification` adds `x_tusker_stream_error` to chat error chunks; `X-Tusker-Cache: bypass` skips caching. None of these today govern response body shape end-to-end. |
| **TODO pressure** | 25 actionable items in `TODO.md`; 6 of them sit on the critical path or adjacent path for normalization (extracting chat-SSE assembly out of `endpoints.py`, standardizing one fingerprint width, fixing test flakes, and proving no provider splice after client-visible output). The rest are independent and can run in parallel. |
| **Recommendation** | Defer the feature 1–2 weeks. First: extract chat-SSE assembly (item 10/11 from TODO) so a normalizer can be applied at one place. Second: standardize the four inconsistent fingerprint widths (item 5 ⚠) and pick one canonical `client_id` for the new layer. Third: implement Phase 0 (read-only `ClientProfile` resolver) and Phase 1 (error-envelope dispatcher) with no shape change. Phases 2-5 (chat SSE, Responses, Anthropic, cache, ops) follow with per-pool canary. |

**The single highest-leverage prerequisite is the chat-SSE extraction in `endpoints.py`.** Without it, the normalizer has to either live in five places or get bolted onto each of the inline `json.dumps` emission sites, which is harder to test and easier to leak through.

---

## 2. `TODO.md` open items (flat, classified)

The auditor's full 25-item inventory, grouped by actionability. Items
marked ★ are on or adjacent to the critical path for the normalization
feature.

### Actionable, code-level (can be fixed by editing this repo)

1. ★ `TestIdempotency.test_deadline_cancellation_releases_processing_claim` flake — fix claim acquisition / lease renewal / abandonment for SQLite + PG. (Suite flakes block verification of any new feature.)
2. Run the full deterministic suite on Python 3.11 and 3.12; retain a 50-slowest-test CI report.
3. Investigate `test_sync_zero_seed_ttl_disables_retention` (possibly slow/blocked, or just an interruption point).
4. Make OpenCode CLI adapter tests hermetic (the 7 warm-service tests fail without an `opencode` binary in CI).
5. ★ Add streaming/state-machine coverage (fragmented tool names, mixed content, failover points, duplicate tool results, finish-reason disagreements) — **adjacent to normalization**: normalization must not break these invariants; coverage should land first or together.
6. ★ One shared timeout budget across qualification / candidate / attempt / fallback / cleanup — design-coupled to item 5; touches the response pipeline.
7. ★ Document fallback-eligible failures; prove no provider splice after client-visible output — **highly relevant**: a UA-normalization layer operates on the same client-visible output and inherits the same guarantee.
8. ★ Follow `docs/architecture-remediation-roadmap.md` (no wholesale rewrite). UA hook-point choice should align with the planned extractions.
9. ★ **Reduce `endpoints.py`** — extract stream assembly, feedback correlation, completion classification, orchestration. **Critical adjacent**: response-shape code targeted by UA-normalization lives here; do the extraction before or alongside adding the normalization layer.
10. ★ Split `passthrough.py` into transport / timeout / cancellation / stream-read / credential components (same rationale).
11. Lower `tools/check_module_size.py` baselines after each extraction.
12. Typed `aiohttp` request keys via a compat layer.
13. Reduce warning baseline to zero; unexpected warnings as errors.
14. Expand Ruff rules (F, B, ASYNC, S, RUF) with baseline — do before large normalization diffs so lint debt doesn't mix into that PR.
15. Coverage / branch-coverage thresholds for routing, streaming, idempotency, persistence.
16. Container vulnerability, K8s manifest, secret scanning.
17. **Distributed-state contract**: inventory/label every state store (request-local → cluster-shared).
18. **Distributed-state contract**: define PostgreSQL-unavailable behaviour (fail closed / degrade / SQLite) — only relevant if normalization state is persisted.
19. Multi-replica PG integration tests (correlation, idempotency, cooldown, ranking).

### Actionable, operational (not code)

20. Re-deploy or delete `tusker_gateway/tools/usb-flap-monitor.{sh,service,timer}` (unit not installed on visor; `AGENTS.md` usb-flap section).
21. Prometheus scrape path: gateway now accepts token, but Prometheus connectivity/storage on visor must complete (memory: gateway token wiring landed 2026-09-29).
22. Smoke-test SSH connectivity (`k8s/smoke_production.sh` helper reachable from CI/agent runs without key exposure).
23. kube-proxy/etcd/scheduler/controller-manager monitors disabled pending kube-prometheus-stack values reconfiguration (in `monitoring`, not this repo).
24. Delete the legacy `unusable-*.json` in `tusker-home` capture dir (trivial).
25. Idle Longhorn claims (pr-agent ×7, legacy tusker-home) cleanup — destructive, needs owner approval (4 claims lack backups).

### Blocked on external dependency or operator decision

- ChatGPT pool credential cleanup (device-code enrollment flow) — blocked on a user-side browser authorization.
- `openai-codex` credential 401s on every call (P1 from 09-26 audit) — re-enroll or delete; the rotator only covers 2 models via `credential_model_exclusions`.
- `opencode-go` monthly quota exhausted (35 code + 12 premium + swarm `deepseek-v4-flash` degraded) — wait for reset or plan upgrade.
- Deliberate bump decision for `claude-code 2.1.300` / `opencode 2.0.29` / `kilo 7.8.8` pins — needs CLI-adapter re-verify + live probe.
- Strength-probe follow-ups: re-run `syn:small:text` after synthetic-429 truncations.

### Net assessment for the normalization feature

Six items sit on or near the critical path (1, 5, 6, 7, 8, 9, 10). The two structural extractions (9, 10) are the single biggest de-risking move. Items 1, 5, 6, 7 land with the feature as either prerequisites or co-shipped test coverage. Items 2-4, 11-19 are independent. The operational items (20-25) are unrelated to the feature.

---

## 3. Response-shape code paths (per surface)

### 3.0 Shared builders (the only centralization today)

| Builder | File:line | Emits |
|---|---|---|
| `sse_frame(data)` | `tusker_gateway/sse.py:41-47` | `data: <json>\n\n` framing for a chat chunk |
| `sse_done()` | `tusker_gateway/sse.py:50-52` | `data: [DONE]\n\n` sentinel |
| `sse_comment(comment)` | `tusker_gateway/sse.py:55-64` | `: keepalive\n` comment heartbeat (rationale at `sse.py:87-95` — bypasses OMP's first-event idle filter) |
| `format_openai_chunk(...)` | `tusker_gateway/sse.py:163-185` | `chat.completion.chunk` skeleton (id/object/model/choices:\[index, delta, finish_reason\]); duplicates `content` into both `delta.content` and `delta.text` for OMP compatibility |
| `format_openai_response(content, ...)` | `tusker_gateway/sse.py:194-214` | non-streaming `chat.completion` skeleton with zero-token usage placeholder |
| `openai_error(message, code=, error_type=)` | `tusker_gateway/errors.py:370-375` | the canonical `{"error": {message, type, code}}` envelope used by chat/responses/media/app middleware and admin error paths |
| `_responses_sse_frame(event_type, payload)` | `tusker_gateway/endpoints.py:5676-5681` | Responses-API SSE framing `event: <type>\ndata: {type, **payload}\n\n` |
| `_sse_frame(event, payload)` (Anthropic) | `tusker_gateway/translators/anthropic.py:501-508` | Anthropic `data: <json>` framing (no `event:` line; Anthropic clients key off `type` in payload) |
| `web.json_response(...)` | endpoints / health / admin | ~60 ad-hoc dict emissions (see per-surface sections) |

**Key structural fact:** there is no single shared response-builder class. `sse.py` framers + `errors.openai_error` are the shared core, but every handler composes its own body dicts inline. A UA normalization layer therefore has one clean SSE chokepoint, one error chokepoint, and ~60 JSON call sites.

### 3.1 `/v1/chat/completions` — non-streaming

`endpoints.py:6287` handler, JSON return at `:7254`. Sequence:
`_validate_chat_body` (`:6029`) → pool fallback → `normalize_response_tool_calls`
(`:6794-6799` → `tool_formats.py:588-750`) → alias echo (`:6801-6803`,
`result["model"] = response_alias`) → `web.json_response(result)` at `:7254`.

Schema: OpenAI **raw dict** — upstream `id`/`object`/`model`/`choices`/`usage` preserved, two modifications:
- tool calls normalized to `{index, id, type:"function", function:{name, arguments}}` and `finish_reason` forced to `"tool_calls"` (`tool_formats.py:743-745`)
- `<think>…</think>` content extracted into `reasoning_content` (`tool_formats.py:638-647`); Bedrock `toolUse` blocks converted (`tool_formats.py:607-632`); XML/DSML tool markup stripped from `content` and all auxiliary reasoning fields (`reasoning_content|reasoning|thinking|analysis|reasoning_details`, `tool_formats.py:653-720`)

Per-client branching already present:
- `format_openai_chunk` duplicates `delta.content` into `delta.text` (`sse.py:174-176`) — OMP tolerates both, other clients ignore.
- Model alias echo only for pool routes; direct passthrough keeps upstream id (`endpoints.py:6801-6803`).
- Native-question envelope selected by client adapter (`question_adapters.detect_adapter`) at `endpoints.py:6432-6440` before dispatch; synthetic question bodies (returned as `native_content_question` at `:6708` or streamed) differ per client (omp/opencode/cline/roo/continue/cursor/claude_code/codex_cli/gemini_cli/aider shapes; `question_adapters.py:28-44`, `render_question_arguments` shapes at `:106-138`).

### 3.2 `/v1/chat/completions` — streaming

`endpoints.py:6310` commit path, `:6717-6746` + `:6863-7234`. Headers: committed `text/event-stream` **before dispatch** (`endpoints.py:6717-6728`), `X-Request-ID`, `X-Accel-Buffering: no`. Immediate first event is an assistant **role-only chunk** built by `format_openai_chunk(role="assistant", model=response_alias)` (`:6729-6730`).

Frame stream (per upstream chunk): either
- Codex/non-iterator dict → `_complete_chat_result_stream` (`endpoints.py:5471-5573`) re-emits the full completion as `chat.completion.chunk` frames: text delta per part (`delta.content` + `delta.text`, `:5495-5509`), tool-call frame (`delta.tool_calls`, `:5511-5541`), then a `finish_reason` frame (`:5546-5559`).
- Native SSE iterator → `_normalize_stream` (`endpoints.py:1026-1858`); legacy variant `_normalize_stream_legacy` (`:674-891`) retained mostly for non-tool traffic. It:
  - splits bundled `[content + finish_reason]` chunks into separate frames (OMP strictness, `:674-700`),
  - promotes `reasoning_content` → `content` when content is null (`:793-805`),
  - adapts legacy `delta.function_call` → `delta.tool_calls` (`:995-1023`),
  - runs `_ToolCallStripper` (`:358-652`) to strip/normalize Hermes/Qwen XML tool markup that leaks into `delta.content`, and emits tool-call rescue frames via `_tool_calls_frame` (`:711-753`) / prose frames via `_content_frame` (`:316-333`),
  - forces a synthesized `finish_reason: stop` frame when upstream omits it (`:885-891`).
  - Native tool-call delta index normalization: `_normalize_native_stream_tool_calls` (`:894-992`).
- Model-alias rewriting in-stream: `_stream_with_model_alias` injected only when `response_alias != target_model` (`:6923-6929`).

Error / post-commit paths emit (in order) an assistant-content error chunk, a stop chunk, and `[DONE]` (`:7150-7204`); `ProviderStreamClosedError` additionally embeds a vLLM/OpenRouter-shaped top-level `error` object on the error chunk (`:7189-7194`) so the OpenAI SDK raises `APIError`. The same trio is duplicated for the outer exception handler at `:7325-7360` and for pre-dispatch `BadRequestError` when the role frame already went out (`:7263-7275`).

Per-client branching already present:
- `X-Tusker-Tool-Qualification` header gates the extra `x_tusker_stream_error` field on error chunks (`endpoints.py:7177-7180`, `:7347-7350`) — a header-driven client-visible schema difference (this is the mechanism a UA normalizer would formalize).
- Heartbeats are comments, not data events, because OMP's idle filter ignores empty chunks (`sse.py:87-95`) — an OMP-specific decision applied globally.

### 3.3 `/v1/responses` (streaming + non-streaming)

`endpoints.py:7374`, impl `:7388-7787`. Requests are translated to Chat (`_responses_input_to_messages` `:5268`, tools `:5201`, tool_choice `:5246`, instructions `:5261`) then re-serialized back.

Non-streaming (`:7715-7753`): `normalize_response_tool_calls` → cache → `web.json_response(_chat_result_to_responses(result, model))` (`:7748-7753`).

`_chat_result_to_responses` (`endpoints.py:5634-5673`) emits:
```
{"id": "resp_<hex>", "object":"response", "created_at", "model",
 "output": [ {type:"message", id:"msg_", role:"assistant", content:[{type:"output_text", text}], status:"completed"}
             | {type:"function_call", id, call_id, name, arguments, status} ...]
 "status": "completed"|"incomplete",
 "usage": {input_tokens, output_tokens, total_tokens}}
```
with conversion of Chat content → `output_text` blocks in `_chat_content_to_responses_output` (`:5398-5421`) and per-choice conversion in `_responses_output_for_choice` (`:5576-5631`). Note: `reasoning_content` from upstream is **dropped** (Chat-only shape) rather than surfaced as a Responses reasoning item.

Streaming (`:7587-7714`): commits SSE headers (`:7588-7598`), writes `response.created` (`:7612-7626`), then `_responses_stream_from_chat` (`:5684-5979`) converts Chat SSE into the Responses event lifecycle: `response.created` → `response.output_item.added` (message or function_call items, `:5737-5849`) → `response.content_part.added` → `response.output_text.delta` / `response.function_call_arguments.delta` → `response.output_text.done` / `response.content_part.done` / `response.function_call_arguments.done` → `response.output_item.done` → `response.completed` (`:5924-5977`). Failures emit `response.failed` via `_write_stream_failure` (`:7402-7421`).

Per-client branching already present: **none** — no UA/header-driven branching on this surface. Model id is echoed as the caller's alias (`:7644-7646`).

### 3.4 `/v1/messages` — Anthropic adapter

`anthropic_adapter.py:535-824`.
- Non-streaming: `_openai_to_anthropic` (`:778-787`) delegates to the shared `translators.translate_response(ANTHROPIC, ...)` (`:96-105`; concrete conversion `translators/anthropic.py:340-…`).
- Streaming: `_AnthropicSSEStreamAdapter` (`:139-198`) wraps OpenAI SSE into Anthropic event frames produced by the translator (frame builders at `translators/anthropic.py:501-620`: `message_start`, `content_block_start`, `text_delta`, `content_block_stop`, `tool_block_start/_delta`, `message_delta`, `message_stop`).
- Errors use their own envelope `_anthropic_error` (`:220-228`, OpenAI-compatible shape `{"type":"error","error":{type,message}}`) streamed as `event: error` after commit (`:792-800`, `:812-820`) and returned as JSON otherwise (`:801-805`, `:821-824`).
- **Tool envelope** is a per-block toolUse representation (see `_anthropic_block_to_openai` / `_anthropic_tool_use_to_openai` at `translators/anthropic.py:68,142` for inbound; outbound emitted by the translator's tool block frame at `:563-596`).

Per-client branching already present: **none** — the Anthropic envelope is a de-facto per-protocol normalizer; no UA branching.

### 3.5 `/v1/embeddings`

`endpoints.py:8233-8327`, builder in `providers/embed.py`. Return shape is OpenAI-compatible `{"object":"list", "data": [...], "model": …, "usage": {"prompt_tokens","total_tokens"}}` regardless of backend shape, normalized in `EmbedHandler.embed` (`providers/embed.py:538-572`). Errors via the shared `openai_error` envelope at `endpoints.py:8313-8316` (Retry-After preserved `:8310`), hard error at `:8320-8326`. **No branching by client.**

### 3.6 `/v1/rerank`

`endpoints.py:8135-8230`, builder in `providers/rerank.py`. Cohere/Hermes `results` shape enforced even when the backend returns Voyage's `data` (`providers/rerank.py:589-591`, `:640-643`, docstring at `endpoints.py:8136-8142`). Errors: same `openai_error` pattern at `endpoints.py:8216-8219` / `:8223-8229`. **No branching by client.**

### 3.7 `/v1/images/*`, `/v1/videos`, `/v1/audio/speech`

`endpoints.py:7941-8010` (images) and `:8070-8132` (video). Builders inside `providers/image_generation.py` / `providers/video.py` return plain dicts (e.g. `{"created": ..., "data": [{"b64_json", "revised_prompt"}]}` per `image_generation.py:554-561`, `:787`, `:992`; video merges `job["b64_json"]` at `video.py:331`), emitted verbatim with `web.json_response` (`endpoints.py:7992`, `:8115`). TTS (`:8013-8067`) returns raw `audio/<mime>` bytes via `web.Response(body=…, content_type=…)`, not JSON. Errors are `openai_error`-shaped (`:7996-8010`, `:8053-8067`, `:8118-8132`). **No branching by client.**

### 3.8 `/v1/models`, `/status`, `/health`, `/ready`, `/metrics`

- `/v1/models`: static OpenAI list envelope `{"object":"list","data":[{"id", "object":"model", "owned_by":"tusker-gateway"}]}` at `endpoints.py:6205`.
- `/health`: `{"status":"ok","version":"0.1.0","commit", "rtk_enabled", "semantic_cache_enabled","state_store","config_runtime_status"}` (`health.py:134-144`).
- `/ready`: success / degraded / failure shapes differing in `status`, `reason`, `primary_pool/route`, `degraded_pools`, `pools` detail, `state_store` (`health.py:159-324`, 503s at `:161, :166, :198-209, :302-313`).
- `/status`: wide diagnostic body composed inline at `health.py:362-419` (pools, catalog, quality, capability DBs, provider usage/capacity, cooldowns, enterprise controls, idempotency, semantic cache).
- `/metrics`: Prometheus text exposition at `endpoints.py:6208-6264`; **not JSON**.
- **No branching by client.** No client-identity label series.

### 3.9 `/admin/*` and `/dashboard*`

`admin.py` emits ~35 inline `web.json_response` calls (list at `admin.py:253-1326`), all with locally-shaped bodies; errors use a distinct `openai_error_shape` (`admin.py:1232-1315`) *plus* the shared `errors.openai_error` in middleware/app (`app.py:443-478` for `/metrics` and gateway auth failures). `_store_unavailable_shape()` (`admin.py:1182, :1200-1229`) is a third admin-only envelope. Sessions/keys management uses the fingerprint-bearing shapes (`admin.py:1241, :1263-1291`). Admin page is HTML (`admin.py:603`). **No branching by client identity.** *Should not be touched by a UA normalizer* — admin is operator-only.

### 3.10 `/mcp` and memory banks

`mcp_guard.py:204-248`. MCP responses use JSON-RPC shape `_jsonrpc` / `_jsonrpc_error` (`mcp_guard.py:130-148`) with JSON-RPC-3 error codes. Memory banks (`memory.py:91-121`) pass through upstream bytes (`memory.py:85-88`) or wrap failures in `{"error": {message, code}}` local shape (`memory.py:76-101`). **No branching by client.** *Should not be touched by a UA normalizer* — both surfaces are server-to-server with their own envelopes.

### 3.11 Where surfaces bypass the shared builders

- `format_openai_chunk` is only used for gateway-authored error/role/finish frames (`:6729, :7173, :7198, :7343, :7354`, plus normalizer finish frames `:708-710`) — regular content frames are re-encoded inline with a manual `json.dumps` at `endpoints.py:840, :847, :852, :854` and `:333, :753` rather than going through `sse_frame(format_openai_chunk(...))`.
- `_content_frame` and `_tool_calls_frame` (`:316-333`, `:711-753`) duplicate the chunk schema with a template obj.
- `_complete_chat_result_stream` (`:5471-5573`) builds chunk dicts inline again with its own id model.
- The Anthropic translator builds its own frames (`translators/anthropic.py:501-620`) — not via `sse.py`.
- The Responses adapter builds its own frames via `_responses_sse_frame` (`endpoints.py:5676-5681`) — not via `sse.py`.
- Admin (`admin.py`) and memory (`memory.py`) responses each have local envelopes not derived from `errors.openai_error` / `openai_error_shape` alone.

**Implication for the planned layer:** a UA-keyed normalizer has to either (a) intercept at 4 physical points (raw dict `json.dumps`, `sse_frame`, `_responses_sse_frame`, translator `_sse_frame`) plus ~30 handler-local emissions, or (b) be applied by each handler's own JSON/SSE commit step. The cleanest approach is to extract chat-SSE assembly first (TODO items 9, 10) so the normalizer can be applied at one place.

---

## 4. Client-fingerprinting surfaces (where identity comes from today)

### 4.1 Inbound `User-Agent` — exactly one consumer

There is **one** inbound UA consumer today:

| Site | What is read | Derived | Used for |
|---|---|---|---|
| `question_adapters.py:80-95` (`detect_adapter`) | Raw `User-Agent`, lowercased | Adapter key via substring markers: `opencode, open-code, cline, roo, continue, cursor, claude-code, codex, gemini, aider` → `QuestionAdapter(key, tool_name, shape, host_permission)` | Approval/question tool envelope shape for native question responses; called once per chat request at `endpoints.py:6432-6440`, stored in a ContextVar (`question_adapters.py:48-50, 97-103`), consumed via `current()` when rendering question arguments (`question_adapters.py:106+`) |
| Same file `:73-78` | `X-Tusker-Harness` / `X-Client-Harness` / body `harness` / `metadata.harness` | Same adapter, but explicit-header precedence over UA | Same; this is the documented trust hierarchy (`question_adapters.py:36-44` explicitly refuses to let UA alone disable the gateway's host-permission guard) |

- **UA is not logged, not echoed, not part of any cache key, quota key, or trace attribute today.** All other `User-Agent` literals in the repo are **outbound spoofed UA** for upstream Cloudflare/API expectations: `auth_strategies.py:173, :238` (codex bypass `codex_cli_rs/0.0.0 (Tusker Gateway)`), `catalog.py:592, :649, :711, :815, :960, :1036, :1116` (catalog refresh), `codex_oauth.py:345, :515`, `providers/capabilities.py:238, :408, :594, :644`, `model_rankings.py:533`, `copilot_enroll.py:228`, `copilot_exchange.py:97, :134, :154` (GitHub Copilot `EXCHANGE_USER_AGENT = "GitHubCopilotChat/0.26.7"`, `copilot_constants.py:19`, `HermesAgent/1.0` default in `copilot_request_headers`).

### 4.2 Auth-key prefix / identity — the de-facto client_id

| Site | What is read | Derived | Goes to |
|---|---|---|---|
| `identity.py:73-87` (`extract_api_key`) | `Authorization: Bearer …` and `x-api-key` headers; rejects conflicting pair | One unambiguous caller credential (no prefix sniffing) | Only credential-extraction entry point; used by `auth.py:73` and re-derived per handler by `endpoints.py:3558-3562` (`_resolve_api_key`) and the idempotency fallback `idempotency.py:306` |
| `identity.py:68-70` (`fingerprint_api_key`) | Raw key | **Full 64-hex SHA-256** of the key — "stable, non-secret identifier used by policy and quotas" | The canonical client identifier; see propagation table §4.6 |
| `auth.py:72-103` (`AuthMiddleware.verify` → `_attach_identity`) | Extracted token | Constant-time comparison against configured keys (`auth.py:83`); `CallerIdentity` resolved and stashed at `request["identity"]` + `request["_api_key_fingerprint"]` (`auth.py:97-103`) | Everything downstream (§4.6) |
| `identity.py:104-163` (`CallerIdentity`) / `:173-189` (`IdentityStore.resolve`) | Full key fingerprint | principal, tenant, scopes, allowed pools/models/providers/memory-banks; unmanaged keys get `CallerIdentity.legacy(fingerprint)` = `principal=f"key:{fingerprint[:12]}"`, `tenant="default"` (`:136-143`) | Policy enforcement (`identity.py:279-334` `authorize_request`), memory scopes (`memory.py:52-60`), guardrail marker (`guardrails.py:402-431`), admin access (`admin.py:103-109, :503-525, :1066-1080`) |
| `config_store.py:1255-1270` and `:505` | DB rows | Rehydrates `CallerIdentity(key_fingerprint=fp, principal, tenant, …)` from `tusker_config_*` tables | DB-backed identity config; key generation uses `sk-` prefix (`config_store.py:1065, :1199`) — the only prefix convention, but **no code branches on the `sk-` prefix itself** |
| `admin.py:320-345` (`admin_providers`) | Provider raw keys | SHA-256 first-16-hex `key_fingerprint` per provider for the admin UI | Read-only diagnostics; never client identity |
| `auth_strategies.py:21-23` (`_token_fingerprint`) | Upstream provider tokens | 12-hex SHA-256 | Logging only (`auth_strategies.py:118-122, :213-217`) — distinguishes *upstream* credentials, not callers |

**No API-key-prefix-based branching exists** (no `startswith("sk-")` behavior switches). Prefixes exist only for key *generation* (`sk-` for client keys, `gho_` comment at `auth_strategies.py:103`).

### 4.3 Session headers

| Site | Headers read | Derived | Used for |
|---|---|---|---|
| `endpoints.py:3565-3593` (`_request_conversation_id`) | `x-opencode-session`, `x-conversation-id`, `x-session-id`, `x-omp-session-id` (in that order), then body `session_id`/`conversation_id`, then `metadata.session_id`/`metadata.conversation_id` | Sanitized string (`_sanitize_opencode_session_id`, `passthrough.py:155-166`: ≤256 chars, no control chars, else `None`) | Stable per-conversation id passed into `PassthroughClient.chat(stream_chat)` as `conversation_id` |
| `passthrough.py:219-237` (`_stable_opencode_session_id`) | Raw caller key (not a header) | Fallback conversation id: `tusker-` + SHA-256 of `{caller: sha256(key)[:16] or "anonymous", opening: first system/developer + first user message}` | Deterministic id when no session header is supplied — **this is an existing UA-less caller+payload fingerprint** |
| `passthrough.py:2588-2635` (upstream rewrite) | `x-opencode-session` in outbound headers | For `opencode-go`/`opencode-zen` (`passthrough.py:83-85`): explicit header else `conversation_id` else `_stable_opencode_session_id(messages)`; normalizes duplicates to exactly one header | Upstream session continuity |
| `endpoints.py:6456-6458`, `:7494-7500`, `anthropic_adapter.py:603-607` | (via `_request_conversation_id`) | `set_conversation_context(conversation_id)` → ContextVar `tusker_approval_conversation` (`native_question.py:66-73`) | Scopes pending approvals to a conversation (`native_question.py:104-114` `_conversation_matches`) |
| `stream_diagnostics.py` + `endpoints.py:7135` (`_conversation_log_id`, `:3596-3600`) | conversation id | 12-hex SHA-256 log id | Stream diagnostics logging |
| `catalog.py:817-819` | Constant `tusker-catalog-opencode-go` | Gateway's own upstream session for catalog refresh | Not a client surface |

Test evidence: `tests/omp_minimax_harness.py:23, :308` rely on one stable `x-opencode-session` per harness run; `tests/test_catalog.py:586` asserts upstream propagation.

Related response-header sessions: `X-Request-ID` echo (`observability.py:233-300`, `endpoints.py:6724, :7594`, `anthropic_adapter.py:686`).

### 4.4 IP / XFF / Cloudflare — only behind trusted proxies

| Site | Header/peer | Derived | Used for |
|---|---|---|---|
| `observability.py:20-39` (`client_ip`) | `CF-Connecting-IP`, then leftmost `X-Forwarded-For`, **only when direct peer is in `TUSKER_TRUSTED_PROXY_RANGES`** (`:41-67`, Cloudflare CIDRs default; `_is_trusted_proxy` `:70-77`); else `request.remote` unchanged | Spoof-resistant real client IP | (a) auth-failure log line `auth.py:94-95`; (b) access-log record `observability.py:183` |
| `observability.py:245` | `X-Request-ID` | Caller-supplied or `req_<16hex>`; truncated to 128 chars | Per-request correlation id; no client-identity meaning |
| `admin.py:477` | `request.remote` only (no XFF trust) | Per-peer login-rate-limit bucket | `/admin/login` brute-force limiter |
| `admin.py:541` | `X-Forwarded-Proto == "https"` | Cookie `secure` flag behind TLS-terminating proxy | Session cookie flag only |

Untrusted-header hygiene: `admin.py:84-100` Origin validation, `X-CSRF-Token` `admin.py:74-79`. No other `CF-*`/XFF reads exist. Client IP is **not** part of any cache key, quota key, or identity decision — IP is the *coarsest* identifier and only surface that can distinguish two requests sharing one API key from different hosts, but it is currently log-only.

### 4.5 Existing caller-fingerprint helpers

| Helper | Location | Output |
|---|---|---|
| `fingerprint_api_key(api_key)` | `identity.py:68-70` | Full SHA-256 hex — canonical client_id |
| `CallerIdentity.legacy(fp)` / `.from_raw` | `identity.py:117-143` | principal/tenant derivation, `key:{fp[:12]}` synthetic principal |
| `make_caller_scope(api_key)` | `cache.py:66-67` | SHA-256 hex namespace for response cache |
| `_stable_opencode_session_id(messages, caller_key=…)` | `passthrough.py:219-237` | `tusker-<32hex>` conversation fingerprint (caller-hash + opening messages) |
| `_conversation_log_id(value)` | `endpoints.py:3596-3600` | 12-hex sha256 log-safe id |
| `set_caller_context(fingerprint)` ContextVar `tusker_approval_caller` | `native_question.py:57-63`; set in `auth.py:86-92` | Approval ownership binding (`_caller_matches`, `native_question.py:100-107`) |
| `_key_fingerprint` in quota modules (independent re-derivations, **note truncation differences**): `budget.py:108-109` (full sha256 — verify per-call), `rate_limit.py:92-93` (**32-hex**, different from 16-hex/64-hex elsewhere) | | Per-key budget/ratelimit bucket keys |

**⚠ Consistency note for the feature:** the codebase currently uses **at least four lengths of the same SHA-256 fingerprint** (64-hex `fingerprint_api_key`, 32-hex `rate_limit.py:93`, 16-hex trace attr `endpoints.py:6348`/`anthropic_adapter.py:581`, 12-hex log principal `identity.py:140`). A normalization layer standardizing a `client_id` should pick one canonical width and derive the short forms from it.

### 4.6 Is a stable per-request `client_id` propagated through the lifecycle?

**Partially — the raw material exists but there is no single `client_id`.** Current propagation matrix:

| Lifecycle stage | Carries key fingerprint? | Carries request_id? | Carries conversation/session id? | Carries UA? | Evidence |
|---|---|---|---|---|---|
| Auth middleware → request dict | ✅ `request["_api_key_fingerprint"]` + `request["identity"]` | — | — | — | `auth.py:97-103` |
| Access log (`tusker_gateway.access`) | ✅ `principal`, `tenant`, `key_fingerprint` (full) | ✅ `request_id` | ❌ | ❌ | `observability.py:175-192` |
| Audit event stream | ✅ same three fields | ✅ `request_id` | ❌ | ❌ | `audit.py:283-307` |
| OTLP trace spans | ✅ `tusker.api_key_fingerprint` (16-hex slice) on the root span of chat (`endpoints.py:6345-6348`) and anthropic messages (`anthropic_adapter.py:578-582`); child spans inherit trace context but not the attribute by default | spans are per-request | ❌ | ❌ | `tracing.py:22-28, 157-184` |
| Approval scoping | ✅ via `set_caller_context` ContextVar | — | ✅ via `set_conversation_context` ContextVar | ❌ | `native_question.py:57-73`, `auth.py:86-92`, `endpoints.py:6456-6458` |
| Idempotency record key | ✅ fingerprint + method + path + `Idempotency-Key` header (`idempotency.py:337`), hashed | — (no request_id) | ❌ | ❌ | `idempotency.py:303-310, 379-403` |
| Response cache key | ✅ `make_caller_scope(api_key)` = sha256(key) | ❌ | ❌ (full message hash instead) | ❌ | `cache.py:66-114`; call sites `endpoints.py:6540-6552, 7508-7530` |
| Semantic-cache scope | ✅ `caller_scope` required (`semantic_cache.py:190-210`), bypass when no key (`endpoints.py:3694-3706` `_semantic_cache_bypass_reason` → `"missing_caller_scope"`) | ❌ | ❌ | ❌ | same |
| Per-key budget / rate-limit | ✅ fingerprint-keyed DB rows | ❌ | ❌ | ❌ | `budget.py:108-166`, `rate_limit.py:92-215` |
| Upstream provider headers | ❌ key fingerprint never sent upstream | `request_id` param exists on `client.chat` (`passthrough.py:2106-2147`) — verify whether it's placed on an outbound header per provider (Codex `originator` UA set comes from fixed constants, not the client) | ✅ `x-opencode-session` for opencode providers (`passthrough.py:2617-2635`) | ❌ | |
| `/status` | ❌ aggregate counts only (`identity_profiles`, `identity_required`) | ❌ | ❌ | ❌ | `health.py:394-416` |
| `/metrics`, `/dashboard` | ❌ aggregation by pool/provider/model/status labels only (`endpoints.py:6320-6327`; no client-label series) | | | | |
| Stream-diagnostics captures | ❌ keyed by `request_id`, not client | ✅ | ❌ | ❌ | `stream_diagnostics.py:140-189` |

**Summary:** `key_fingerprint` is the de-facto client_id and reaches logs, audit, traces (16-hex), caches, quotas, idempotency, and approvals — but not `/status`, not metrics labels, and not upstream headers. `request_id` is per-request (not per-client) and reaches logs, audit, SSE headers, and stream captures. UA currently reaches only the approval-envelope adapter. **No existing single stable per-(client,+UA,+session) identifier exists; the closest composition point is the auth middleware**, which already sees the key and could additionally read UA/session headers to compute one.

### 4.7 Natural hook points for a UA+key+fingerprint normalization layer

1. **`AuthMiddleware.verify` / a new middleware adjacent to it** (`auth.py:72-103`): the only place that already resolves both the key and (via `request.headers`) any header. Computing `request["client_profile"]` here gives every downstream consumer one attribute to read; middleware order at `app.py:611-628` (request-id → audit → auth → authorization → deadline → idempotency) is compatible.
2. **`fingerprint_api_key` (`identity.py:68`)**: extend or wrap to accept UA/session inputs and emit a composed client_id; every fingerprint already flows from this function, so policy/quotas stay consistent.
3. **`question_adapters.detect_adapter` (`question_adapters.py:69-95`)**: the precedent for header-precedence + UA-sniff resolution; a client-profile resolver should mirror its `X-Tusker-Harness` > body `harness` > UA ordering and reuse the ContextVar pattern (`question_adapters.py:48-50`). Called at `endpoints.py:6432-6440`.
4. **`_request_conversation_id` (`endpoints.py:3565-3593`)**: already composes 4 headers + body fields + a caller-keyed fallback; the right place to blend session identity into the client_id for conversation-scoped normalization.
5. **`observability.AccessLog` + spans**: add `client_id`/UA-class fields at `observability.py:176-192` and the span attribute at `endpoints.py:6345-6348` for cross-request analytics.

---

## 5. Risks

### 5.1 Engineering risks

1. **Five independent frame encoders** (§3.11) — a hook only at `sse_frame` will silently miss the inline `json.dumps` copies the normalizer emits and the Anthropic/Responses/SSE translators. Normalization must be applied *before* those encode, or those four need explicit call-through.
2. **Cache poisoning across client shapes.** Cached chat results (`endpoints.py:6829`) and semantic-cache stores (`:6850-6855`) are keyed on pool/model/messages/tools/caller-scope but *not* on client fingerprint; if two UA-classes expect different shapes and one normalizes at write time, the cache can hand shape-A clients a shape-B body. **Mitigation:** include normalized-shape id in `make_cache_key` (`cache.py`) or normalize at read time, not write time.
3. **Streaming post-commit failures:** once the 200/SSE headers are committed, the gateway can no longer return a JSON error; UA-specific error bodies must be produced *inside* the post-commit error trio (`:7150-7204` and the duplicated block at `:7325-7360` and `:7263-7271`), or UA-classes that expect a top-level `error` object will lose error information.
4. **OMP's client-specific quirks are load-bearing, not cosmetic:**
   - `delta.content`+`delta.text` duplication (`sse.py:174-176`) is required for OMP to render assistant text from reasoning-only and rescue-frame outputs.
   - SSE comments for heartbeats rather than empty chunks (`sse.py:87-95`) are needed to keep OMP's idle timer alive; emitting them as data events would *break* OMP's first-event watchdog per `sse.py:87-95`.
   - The role-only first frame before dispatch (`endpoints.py:6729-6746`) is what OMP uses as its first-event signal (`endpoints.py:6310-6314`). Any UA normalizer that drops or reshapes it must preserve the "first event arrives quickly" behavior.
5. **`reasoning_details` vs `reasoning_content` overlap** (`tool_formats.py:653-742`): a strict normalizer could drop `reasoning_details` for clients that don't read it; the current code only pops it when it duplicates sanitized text, so a broad cleanup must verify OpenRouter's parallel field isn't load-bearing for current clients.
6. **Post-commit `ProviderStreamClosedError` vLLM shape has a client-visible branch** (`endpoints.py:7189-7194`): if per-client shapes diverge, which clients get `error` vs `x_tusker_stream_error` must be explicit per UA-class, not silently inherited from today's header-only flag.
7. **`format_openai_chunk` model default is "tusker-gateway"** (`sse.py:171`) and error/role frames hard-code it too (`:7173, :7343`); a UA normalizer that rewrites `model` must decide whether error frames keep the gateway's own name (today's behavior) or the client-facing alias (chat content frames' behavior at `:7173` vs `:6801-6803`).
8. **Inconsistent fingerprint widths** (§4.5 ⚠): standardizing on one canonical `client_id` requires deciding whether existing persisted keys (budget `budget.py:166`, ratelimit `rate_limit.py` tables, cache rows, idempotency records) migrate or keep their legacy widths — DB schemas key on them (`PRIMARY KEY (fingerprint, …)` at `budget.py:148`).

### 5.2 Trust / safety risks

1. **UA spoofability is a known, deliberate stance** — `question_adapters.py:36-44` documents that UA alone must never weaken the gateway's safety posture; the same applies to response normalization when the shape difference encodes *capability* assumptions (e.g., whether a client can render `reasoning_content`). The trust hierarchy (explicit header > body harness > UA) must be preserved.
2. **Session-header spoofability** — `x-opencode-session` etc. are client-supplied and only sanitized for length/control chars (`passthrough.py:155-166`); fine for conversation scoping, but must not be trusted as *identity* beyond pairing with the authenticated key fingerprint.
3. **Adapter detection is spoofable by headers/UA today** and the repo explicitly chooses host-side permission gating over trusting the UA. Any *safety-visible* branching (approval envelopes, high-impact gating) must keep the explicit-header precedence and must not weaken to UA-only trust.
4. **Key rotation changes the client_id** — fingerprint is SHA-256 of the raw key, so `POST /admin/keys/{fp}/rotate` (`admin.py:1296-1302`) invalidates any normalization state keyed on it; consider keying UA-normalization on principal/tenant instead of raw-key fingerprint, or accepting the reset.
5. **Legacy synthetic principals are collisiony** — `CallerIdentity.legacy` uses `key:{fp[:12]}` (`identity.py:140`); two different keys sharing a 12-hex prefix produce the same display principal. Full 64-hex fingerprint must be the join key; short forms only for display.
6. **Trusted-proxy gate matters for IP-based fingerprinting** — `client_ip` returns spoofable headers only behind Cloudflare CIDRs (`observability.py:20-39`); if the normalizer ever includes IP in the fingerprint, deployments with an in-cluster ingress must override `TUSKER_TRUSTED_PROXY_RANGES` or the component silently degrades to peer IP.

### 5.3 Operational / observability risks

1. **Missing client_id on `/status` and `/metrics`** — if per-client normalization is deployed, operators will want a per-client-classes series in metrics labels and a per-class health block in `/status`; today there is nothing to hang it on (`endpoints.py:6320-6327`, `health.py:394-416`). Plan to add it as part of Phase 0.
2. **Admin & memory surfaces are out of scope by default** — they use independent envelopes (`admin.py:1182, :1232-1315`, `memory.py:76-101`) and the fingerprint layer should not silently claim to affect them.
3. **No CI signal that a per-UA shape change didn't regress a different client.** Until there's a per-UA fixture matrix in `tests/`, any change to the shared chunk/error builders can ship broken for the client you didn't test.

### 5.4 What to explicitly NOT do

- **Do not** key normalization on UA alone. The header/UA/body precedence is load-bearing for safety and must be preserved.
- **Do not** normalize on write into the cache — read-time only, or include `shape_id` in the cache key. Otherwise two clients with different expected shapes can poison each other.
- **Do not** drop the role-only first frame, the `delta.text`/`delta.content` duplication, or the SSE-comment heartbeats without per-UA verification — they are OMP's first-event signal and idle watchdog.
- **Do not** touch `/admin/*`, `/mcp`, `/v1/memory/*`, `/v1/audio/speech` (binary), or `/v1/models` — they have their own envelopes and no per-client schema today.
- **Do not** re-key the budget, rate-limit, or idempotency tables on a new client_id. Their schema is `PRIMARY KEY (fingerprint, …)` and changing that requires a migration and an online cutover.
- **Do not** use IP as a fingerprint input without first extending the `TUSKER_TRUSTED_PROXY_RANGES` check; in-cluster deployments would otherwise degrade to peer IP silently.

---

## 6. Phased plan

The plan assumes the feature is approved after this survey. Each phase is shippable and reversible; later phases don't depend on earlier ones being 100% deployed (the read-only `ClientProfile` resolver and the error dispatcher are usable independently).

### Phase 0 — Fingerprint resolver (no behavior change)

- New module (or extend `question_adapters.py`) that resolves a `ClientProfile` from `X-Tusker-Harness` / `X-Client-Harness` / body `harness` / `User-Agent` / `Authorization` key hash, with `detect_adapter`-style aliases. No shape changes yet.
- Pick a canonical fingerprint width (recommended: full 64-hex, with 32-hex and 16-hex truncated forms for log/trace). Decide whether to migrate existing DB rows or leave them on legacy widths.
- Add a `client_profile` ContextVar mirroring `question_adapters.set_current` / `current` (`question_adapters.py:47-103`) so handlers can read it without threading args.
- Set the profile in `AuthMiddleware.verify` (`auth.py:72-103`) so every request that reaches a handler already has one.
- Add `client_profile` to the access log (`observability.py:175-192`) and the chat / anthropic root span attribute (`endpoints.py:6345-6348`, `anthropic_adapter.py:578-582`).
- Add a `/status` block: per-`client_profile` count of active requests and circuit-breaker state. (The labels are already plumbed; the aggregation function is what's missing.)
- Tests: deterministic per-UA / per-key / per-session fingerprint; precedence rules; `client_profile` propagation through one chat round-trip; idempotency-key composition.

### Phase 1 — Error-envelope normalization (lowest risk)

- Introduce a per-protocol error builder dispatch keyed on the client profile: `openai_error` (chat/responses/media/admin-JSON), `_anthropic_error` (messages), `_jsonrpc_error` (mcp), `openai_error_shape` (admin) already exist; the change is to route through one dispatcher rather than hard-coding per handler.
- Validate against existing tests (`tests/test_fault_scenarios.py` post-commit error frames; AGENTS.md tests section).
- Goal: every error returned to a client goes through one function whose body adapts to `client_profile`. Net effect: error shape becomes configurable per client without per-handler changes.

### Phase 2 — Chat SSE normalization (highest value, highest test burden)

**Prerequisite:** complete the `endpoints.py` extraction work (TODO items 9, 10) so the chat-SSE builders are isolated in one module. The normalizer is then applied at one place.

- Centralize the four chat-SSE encoder variants (inline `json.dumps` in `_normalize_stream*` at `endpoints.py:840/:847/:852/:854`, `_content_frame`, `_tool_calls_frame`, `_complete_chat_result_stream`) behind a single `chat_chunk_bytes(chunk_dict, profile)` helper that calls `format_openai_chunk` / `sse_frame`.
- Introduce per-profile `delta.content`→`delta.text` duplication policy, `reasoning_content` promotion policy (currently unconditional at `endpoints.py:793-805`), tool-call promotion/rescue policy, and `finish_reason` synthesis policy.
- Keep SSE-comment heartbeats unchanged for all clients until a client is known to break on them (`sse.py:55-64`).
- Update the post-commit error trio (`:7150-7204`, `:7325-7360`, `:7263-7271`) to call the dispatcher; per-profile shape for `error` vs `x_tusker_stream_error` becomes explicit.

### Phase 3 — Responses + Anthropic parity

- Route `_responses_sse_frame` / `_chat_result_to_responses` and the Anthropic translator through the same profile resolver. Decide whether to surface `reasoning_content` as a Responses reasoning item for clients that want it (currently dropped, see §3.3 note).
- Add per-profile test coverage in `tests/test_responses_streaming.py` and `tests/test_anthropic_messages.py`.

### Phase 4 — Cache-safety integration

- Fold a `shape_id` (profile-derived) into `make_cache_key` / `make_semantic_scope` OR normalize at response time (never write time) so cross-client cache hits stay safe. Test both paths (`endpoints.py:6582-6601`, `:6819-6829`, `:7508-7530`, `:7642-7749`).
- Document the choice (read-time normalize vs. key-tag) in `docs/architecture-remediation-roadmap.md`.

### Phase 5 — Ops rollout

- Per-pool canary. Start with pools that have no streaming tool traffic. Enable the `ClientProfile` resolver and the error dispatcher in shadow mode first (log the would-have-been shape but emit the current shape), then flip one pool at a time.
- `/metrics` gains a per-`client_profile` series for error and shape-mismatch counts.
- Doc updates: `docs/feature-matrix-and-plan.md` (roadmap), `AGENTS.md` (runtime notes), `docs/enterprise-controls.md` if the new behavior is part of the enterprise contract.

### What I recommend the user decide before approving Phase 0

1. **Pick the canonical fingerprint width.** 64-hex, or stick with the existing 16-hex span attribute and use a longer one only in the access log?
2. **Pick the precedence rule for `client_profile`.** Recommended: `X-Tusker-Harness` > body `harness` > UA + key fingerprint. Same as `question_adapters.py:69-95`. Confirm.
3. **Pick the set of profiles.** `omp`, `opencode`, `claude-code`, `kilo`, `codex_cli`, and an explicit `default` for everything else. Anything else?
4. **Pick the deployment posture.** Shadow mode (log-only) for one release, then per-pool canary, or straight to per-pool canary?

---

## 7. Evidence trail (this investigation)

| | |
|---|---|
| Surveyed | 233 lines of gateway source, 25 actionable TODO items, 8 existing per-client touch points, 5 client-visible frame encoders, 4 fingerprint widths in active use. |
| Methods | `read_file`, `grep_files`, `find_files` against `/Volumes/dev/dev/tusker-ai-gateway/`. Read selectively, not whole files. Three parallel research agents. |
| Read-only | No source modified. Only `docs/investigation-2026-10-16-ua-normalization.md` created. |
| Not deployed | Per delivery-mode `plan-and-commit-no-deploy`. Report will be committed locally and not pushed. |
| Next action (only if user approves) | Phase 0 implementation. Until then, no work. |
