# Local CLI notes — OMP and OpenCode

What the two local coding agents already do for context/token efficiency, and
which gaps the gateway changes can or cannot fill. Facts below were read from
the installed binaries and configuration on 2026-11-24, not inferred.

## OMP (`omp` 19.1.1, `~/.omp/agent/config.yml`)

### What OMP already has

OMP is already far ahead of the gateway on context management. From
`omp config list`:

| Setting | Value | What it does |
|---|---|---|
| `compaction.enabled` | `true` | Automatic compaction when context grows. |
| `compaction.thresholdPercent` | `75` | Percentage trigger. |
| `compaction.modelThresholds` | `{"tusker-gateway/hermes-code": "f950000", "tusker-gateway/hermes-privacy": "f255000"}` | **Fixed** token triggers per model (`f<tokens>` form). |
| `compaction.modelThresholdsEnabled` | `true` | The above overrides the percentage. |
| `compaction.midTurnEnabled` | `true` | Threshold check at safe mid-turn tool-loop boundaries, before the next provider request. |
| `compaction.asyncEnabled` | `true` | Speculative background summarization as context approaches the threshold. |
| `compaction.methodOrder` | `["remote","snapcompact","handoff","shake","soft"]` | Fallback chain for compaction methods. |
| `compaction.supersedeReads` | `true` | Prunes older read results when the same file is re-read. |
| `compaction.dropUseless` | `true` | Prunes tool results flagged contextually useless once consumed. |
| `compaction.keepRecentTokens` | `20000` | Recent history kept verbatim across a compaction. |
| `compaction.remoteEndpoint` / `remoteEnabled` | — | Remote compaction models. |
| `providers.cacheWarming` | `idle` | Refreshes the last request's prompt-cache entry before it expires. |
| `providers.cacheWarmingStrategy` | `auto` | Whether to compact or let the cache expire once warming stops paying. |
| `providers.cacheWarmingCompactPercent` | `60` | Share of the window at which a long tool call may be bridged with compaction. |
| `tools.artifactSpillThreshold` | `20000` | Tool output above this size is spilled to an artifact file; head+tail kept inline. |
| `display.showTokenUsage` | `false` | Per-turn token usage is available but **off**. |
| `statusLine.contextLine` | `embedded` | Context usage is shown in the status line. |

**Conclusion: OMP does not need a `ctxwarn`-style hook.** It has the
threshold, the mid-turn check, the async pre-summarization and the status-line
readout. Installing a token-diet-style transcript-scanning hook would
duplicate a better-integrated mechanism and add a second source of truth.

### Gaps the gateway can close for OMP

**1. The fixed thresholds are wrong for the pools they name.** OMP's
`f950000` / `f255000` are absolute token counts. The gateway's privacy pool
(`TUSKER_POOL_PRIVACY`) declares `context_window: 128000` and its routes are:

| Route | Declared window |
|---|---|
| `mlx-mac/qwen3.8-27b` | 262144 (operator override) |
| `mlx-mac/shirdel-coder-9b-claude-fable-5:latest` | 262144 (operator override) |
| `synthetic/hf:deepseek-ai/DeepSeek-V4.1-Flash-1017` | unknown (no declaration) |
| `apim/gpt-5.6-luna` | 128000 |

With a 255000 fixed trigger and a 128000-token route, OMP would keep sending
until the **gateway** rejects the request with `context_length_exceeded` —
exactly the hard-failure path the new advisory headers are meant to make
unnecessary. The gateway's own pool selection already skips routes that
cannot fit the prompt, so the practical effect is a late 400 rather than a
provider-side truncation, but the client learns about it the expensive way.

**Recommendation:** OMP's `compaction.modelThresholds` for
`tusker-gateway/hermes-privacy` should be a *percentage* (e.g. `80%`) rather
than a fixed count, so it follows whatever route the pool selects. OMP's own
parser accepts `"80%"` (see the value-parsing error message in the binary:
*"a positive integer token count (e.g. 90000), a fixed trigger (e.g.
\"f90000\") or a percentage in (0, 100] (e.g. \"80%\")"*). The same applies to
`hermes-code` (`f950000`) — the code pool contains routes with windows from
8192 (`local-llm/qwopus-9b-coder-mtp`) to 1,050,000 (`apim/gpt-5.6-luna`).

**2. OMP does not read the new advisory headers.** The gateway now emits
`X-Tusker-Context-Estimate` / `-Window` / `-Remaining` / `-Used-Pct` /
`-Warning` (see `docs/context-efficiency.md`). A grep of the `omp` binary
shows no `X-Tusker-*` string, so OMP ignores them today. The headers are
therefore useful to *other* clients today and to OMP only if OMP grows
support. This is a client-side change, not a gateway one.

**3. `display.showTokenUsage: false`.** OMP can show per-turn token usage and
it is off. Turning it on is a one-line config change and directly serves the
"see what you actually spend" goal — cheaper than any new instrumentation.

### OMP commands that already overlap with token-diet

- `omp toks INPUT` — counts a file or text with every embedded offline
  tokenizer (OpenAI, Claude, Qwen…). This is a better `ctxwarn` estimator
  than `chars/4`.
- `omp compress FILES` — rewrites a file into a dense prompt register and
  reports what it drops. This is `token-diet strip`, already built.
- `omp bench` — benchmarks TTFT/prefill vs decode, including prompt-cache
  workloads.

**Applied (2026-11-24):** both recommendations above were approved in-session
and written through `omp config set`: `compaction.modelThresholds` for
`tusker-gateway/hermes-code` and `tusker-gateway/hermes-privacy` are now
`"80%"` (was `f950000` / `f255000`), and `display.showTokenUsage` is `true`.
Changes persist in `~/.omp/agent/config.yml`.

## OpenCode (`opencode` v2.3.2)

### What OpenCode already has

- `opencode stats --days N` — token totals, session counts, tool success
  rate. The installed instance reports *849m tokens over 19 sessions in 7
  days* with a `--models` breakdown. That is token-diet's `budget status`
  requirement met, with real numbers.
- Plugin system (`opencode plugin add/list`) with a JSON hook manifest
  (`hooks.json`) supporting `SessionStart`, `UserPromptSubmit` and `Stop`
  command hooks. The installed `hindsight-coding-agents` plugin uses exactly
  that mechanism.
- Plugin JS API events observed in the installed plugin:
  `chat.message`, `experimental.chat.messages.transform`,
  `session.idle`, `session.execution.succeeded/failed/interrupted`.

### What OpenCode lacks

- **No context-pressure warning.** There is no `ctxwarn` equivalent: nothing
  tells the session it is approaching its window, and `experimental.chat.
  messages.transform` is the natural hook point if one is wanted.
- **No document interception.** `docextract` has no counterpart; a `Read` of
  a PDF in OpenCode goes to the model as raw bytes.

### Where the gateway change helps OpenCode

The gateway's Anthropic-document rejection (§2 of
`docs/context-efficiency.md`) is exactly the right behaviour for OpenCode
users: OpenCode talks to the gateway, and a PDF that used to become ~468k
tokens of escaped base64 now produces an actionable 400. OpenCode can then
fall back to reading the file as text.

### Where it does not

OpenCode's plugin API cannot see the gateway's response headers through the
plugin surface, so the new advisory headers are not directly reachable from a
plugin either. `session.idle` plus a local token estimate would be the
opencode-native equivalent of `ctxwarn` — but note the same caveat as for
OMP: OpenCode already reports real token totals via `opencode stats`.

## Recommended local changes (no gateway involvement)

| Change | Where | Why |
|---|---|---|
| Replace fixed compaction thresholds with percentages | `~/.omp/agent/config.yml` → `compaction.modelThresholds` | The gateway's pools contain routes with windows from 8k to 1.05M; a fixed count cannot be right for all of them. OMP accepts `"80%"`. |
| `display.showTokenUsage: true` | same file | Per-turn token usage is already implemented and off. |
| Nothing for `ctxwarn` | — | OMP's own threshold + mid-turn + async compaction already covers it. |
| Nothing for `docextract` in OMP | — | The gateway now rejects documents with an actionable error; the client-side fix is to read text. |
| Consider a `session.idle` context-pressure hook for OpenCode | a new opencode plugin | Only if per-turn in-session pressure feedback is wanted; `opencode stats` already covers the retrospective view. |

## Not recommended

- Installing token-diet's hooks (Claude Code `PreToolUse`/`PostToolUse`
  shims) into OMP or OpenCode: both agents have their own, better-integrated
  mechanisms, and the token-diet hooks scan the transcript with `chars/4`
  while OMP ships real tokenizers.
- Registering Serena/ICM/Context7 as gateway-side MCP servers. They are
  agent-side tools; the gateway's `/mcp` endpoint is an approval guard, not
  a tool server.
