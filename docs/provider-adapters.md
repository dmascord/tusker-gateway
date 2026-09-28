# Native provider adapters

The `tusker_gateway.provider_adapters` registry is for provider transports
that are not OpenAI-compatible HTTP endpoints. Each adapter accepts the
gateway's normalized chat request and returns an OpenAI chat completion (or
an async iterator of OpenAI SSE frames). The result continues through the
same tool-contract, high-impact audit, and stream validation path as HTTP
providers. Adapters are code-registered; request data and provider config
cannot choose an executable or import arbitrary code.

## Claude Code CLI

The built-in `claude-code-cli` adapter invokes the official `claude -p` CLI
using the runtime's existing Claude Code login. It does not inspect, copy,
refresh, or persist Claude credentials. It is not a ZDR route and is not in
the privacy pool. Production explicitly includes `claude-code-cli/sonnet` in
the code pool; the currently configured `claude.ai` consumer login retains
requests according to Anthropic's consumer data policy.

To opt in, install and authenticate Claude Code in the gateway runtime, then
set `TUSKER_CLAUDE_CODE_ENABLED=true`. The executable defaults to `claude` on
`PATH`; `TUSKER_CLAUDE_CODE_PATH` can select an operator-managed executable,
and `TUSKER_CLAUDE_CODE_TIMEOUT_SECS` controls the process timeout (default
600 seconds). Models are explicitly allowlisted as `sonnet`, `opus`, and
`haiku`, for example `claude-code-cli/sonnet`.

The adapter disables Claude's built-in tools and disables session persistence.
Client-supplied OpenAI function tools are exposed through a request-scoped MCP
proxy; when Claude invokes one, the adapter stops the CLI and returns an
ordinary OpenAI `tool_calls` response for the connected harness to validate,
approve, and execute. On the next turn, the assistant call and client tool
result are replayed as history. Tool-bearing runs use `dontAsk` with a strict
MCP config and allow only this gateway MCP server; text-only runs use plan
Text and image content are supported when the upstream provider accepts it
(currently base64 image data URLs are forwarded to Claude Code's
``stream-json`` input; remote URL fetch is rejected because it depends on the
target site's robots.txt). Other multimodal content such as audio or video is
still rejected rather than silently dropped. Streaming requests use Claude Code's
`stream-json` partial-message output and forward assistant text deltas as
OpenAI SSE while the CLI is still running. Lifecycle and diagnostic events
are not exposed as assistant text; CLI errors after a stream starts are sent
in-band. Gateway heartbeat behavior keeps the client connection active while
the CLI is waiting for its first text delta.

Because the CLI is one-shot, a model that repeats a tool call whose result is
already present in the replayed history is not handed back to the harness a
second time: the adapter re-runs the CLI without tools and answers in prose
instead, and raises `tool_call_loop` when that produces no assistant text.

The gateway image includes a pinned Claude Code CLI executable (currently
2.1.281). The image does not include or provision credentials. In production,
the CLI reads its own runtime login from `CLAUDE_CONFIG_DIR` or
`$HOME/.claude`; do not copy a developer's macOS Keychain into the image.
Claude Code's self-updater is disabled in the image so the pinned version is
stable. Operators must assess Anthropic account/CLI terms and runtime
credential handling before enabling it. Without the environment opt-in,
requests to this provider are rejected as disabled.

OAuth lifecycle belongs to Claude Code, not the gateway adapter. On Linux,
Claude Code keeps its login under `$HOME/.claude/.credentials.json`; that
runtime directory must be mounted with private permissions and writable by
the gateway user. Anthropic documents `CLAUDE_CODE_OAUTH_REFRESH_TOKEN` plus
`CLAUDE_CODE_OAUTH_SCOPES` as inputs to `claude auth login` for automated
credential provisioning. `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`
is a separate long-lived access token and must be replaced when it expires.
The adapter intentionally does not read Keychain data, exchange refresh
tokens itself, or log/store OAuth credentials.

### Login, renewal, and user notification

An operator can start the standard interactive Claude subscription login from
an operator-controlled terminal with `k8s/claude-code-login.sh`. It runs
`claude auth login --claudeai` in the gateway container, where the mounted
Claude runtime home persists the credential. Complete any browser or one-time
code step only in that terminal; never put credentials or login codes in a
chat request. The helper prints only an allowlisted status after login.

`GET /admin/claude/auth` reports only `authenticated`, `login_required`, or
`unknown` (and a small allowlist of auth-method labels), protected by the
existing admin access controls. Claude Code remains responsible for refresh
where supported and may request a fresh login when the current credential
cannot be renewed. The gateway does not promise unattended refresh for every
account type. If an actual request receives an explicit auth-expiry failure,
the gateway returns an OpenAI-compatible HTTP 503 error with code
`claude_auth_required` and directs the client to ask an administrator to run
the login helper. Non-streaming requests receive this as an HTTP error;
streaming requests receive it in-band if Claude reports it after the SSE
response has started.
Auth expiry is not recorded as provider/model health failure or quarantine.

## OpenCode CLI

The `opencode-cli` adapter drives `opencode run --format json`, either against a
long-lived server or (see "Warm server mode" below) an isolated per-request
`--standalone` server, and uses OpenCode Zen model IDs (`opencode/<model>`); a
short model name such as `big-pickle` is expanded to `opencode/big-pickle`. Set
`TUSKER_OPENCODE_CLI_ENABLED=true` to enable it. The executable defaults to
`opencode` on `PATH`, with `TUSKER_OPENCODE_CLI_PATH` and
`TUSKER_OPENCODE_CLI_TIMEOUT_SECS` available for operator overrides. The CLI
uses its existing OpenCode login by default. An explicit `OPENCODE_API_KEY`
is forwarded to the child. Deployments may deliberately set
`TUSKER_OPENCODE_CLI_API_KEY` to provide the CLI-specific credential; the
adapter maps only that explicit variable to `OPENCODE_API_KEY` in its child.
It never silently aliases the gateway's `OPENCODE_ZEN_API_KEY`.

Each request runs from a private temporary working directory with project
configuration and default plugins disabled. Text-only requests do not inject
custom OpenCode config. Tool requests use MCP-only inline config with
request-scoped gateway proxies; the proxy returns calls to the connected
harness and never executes them. Do not add custom OpenCode `permission`
rules here: testing showed that they make OpenCode Zen return HTTP 403
("free tier can only be used from within OpenCode"). The CLI's own
non-interactive permission handling remains in effect for other tools, so
operators should also review any global CLI configuration used by the gateway.
Follow-up tool calls and results are replayed as transcript history. The
gateway image includes the pinned OpenCode v2 CLI executable (currently
2.0.15). Production explicitly enables this adapter and includes
`opencode-cli/big-pickle` in the code pool.

Set `TUSKER_OPENCODE_CLI_PATH` when an operator-managed installation should
be used instead. OpenCode Zen may still reject CLI/API use based on account or
service policy; the adapter does not bypass those restrictions.
For streaming requests, JSON text events are forwarded incrementally as
OpenAI SSE; other CLI lifecycle/diagnostic events are not presented as model
content, and the gateway heartbeat remains active while awaiting output.

### Warm server mode

By default (`TUSKER_OPENCODE_WARM_ENABLED=1`) the adapter keeps one
`opencode serve` alive and sends requests through it instead of paying a private
server start per request. Measured in production, a follow-up request drops from
roughly 8-10 seconds to about 5; the first request after the server is released
pays that startup once (about 6 seconds).

The server fixes its MCP configuration when it starts, so tool requests cannot
use per-request inline config. The adapter publishes each request through the
same broker pointer the Kilo warm path uses
(`TUSKER_OPENCODE_WARM_BROKER_DIR/active.json`): the bridge that the server
spawned resolves the manifest and call file per tool call. That pointer names
exactly one request, so warm requests are serialized - a concurrent OpenCode
request queues behind the in-flight one instead of sharing a bridge.

Request lifecycle:

- the adapter starts a server, or adopts the one it recorded, and replaces any
  other server so the pointer contract holds;
- each request runs `opencode run --format json` from a private empty working
  directory and streams its JSON events;
- a tool call is detected from the pointer's call file, generation is
  interrupted (`POST /api/session/{id}/interrupt`), the child is stopped, and the
  call returns to the client;
- the session is deleted (`DELETE /api/session/{id}`) so the server's session
  store and its ephemeral storage stay bounded;
- `TUSKER_OPENCODE_WARM_IDLE_SECS` (default 300, `0` disables) releases the
  server once traffic stops.

The server is a child of the gateway and shares its cgroup: expect roughly
160 MiB resident while warm, so the container memory limit must leave room for
it plus one transient `opencode run` client.

If the server cannot start, or a non-streaming request fails inside it, the
adapter logs a warning and falls back to the isolated `--standalone` path. A
streaming failure after the first frame propagates, because the client already
holds partial output. `TUSKER_OPENCODE_WARM_ENABLED=0` disables the path
entirely.

### Which adapters can keep a server warm

| Adapter | Warm path | Basis |
|---|---|---|
| `kilo_cli` | yes, in the worker pod | `kilo serve` publishes the HTTP API the broker pointer addresses |
| `opencode_cli` | yes, child of the gateway | `opencode serve` plus the same broker pointer (above) |
| `claude_code` | no | no request server exists to keep warm, and CLI start is not the cost |

`claude_code` deliberately has no warm path. Evidence from the production
container (2026-09-28):

- Claude Code 2.x is a native binary - the image copies
  `claude-code-linux-${arch}/claude`, not a Node entrypoint. `claude --version`
  measured 0.01s and full CLI init (`claude --help`) 0.14-0.15s, against a
  roughly 6s OpenCode server start and 1.3s for `kilo --help`. There is no
  startup large enough for a warm server to remove. MCP configuration loading is
  the largest CLI-side cost (`claude mcp list` measured 2.8s), but the adapter's
  tool requests use request-scoped proxies that a shared process cannot carry.
- The CLI exposes no `serve` or daemon subcommand. `claude --help` lists
  `agents`, `attach`, `auth`, `auto-mode`, `doctor`, `gateway` (an enterprise
  auth/telemetry gateway), `import`, `install`, `logs`, `mcp`, `plugin`,
  `project`, `respawn`, `rm`, `setup-token`, `stop|kill`, `ultrareview`, and
  `update`. `--bg` starts a *session* in the background addressed by a session
  id (`claude attach`, `logs`, `stop`, `rm`) - a conversation handle, not a
  request server with a stable endpoint.
- Each Claude request needs its own MCP proxy and call file so the client's tool
  call returns to that request (see "Claude Code CLI" above). A background
  session cannot carry a per-request bridge, so reusing one would break the
  tool-call contract that `kilo_cli` and `opencode_cli` satisfy through the
  pointer file.

If Claude Code latency ever needs work, the levers are session reuse
(`--resume`) or provider selection, not a warm server.

## Kilo Code CLI

The `kilo-cli` adapter runs `kilo run --format json --model provider/model`.
It accepts explicit Kilo model IDs, including nested upstream names (for
example, `kilo-cli/anthropic/claude-sonnet-4.6` or
`kilo-cli/groq/openai/gpt-oss-20b`), and is opt-in with
`TUSKER_KILO_CLI_ENABLED=true`. `TUSKER_KILO_CLI_PATH` chooses the executable
and `TUSKER_KILO_CLI_TIMEOUT_SECS` sets the request timeout (default 600
seconds). The adapter uses Kilo's existing runtime auth; if `KILO_API_KEY` is
set, it is passed only to the child process. For a direct provider/model ID,
the gateway passes only that provider's configured API key under the provider's
standard CLI variable (for example, the Groq key for `groq/...` models), never
the full gateway key set.

Each request receives high-priority inline `KILO_CONFIG_CONTENT`, disables
project config, denies tools by default, and exposes only request-scoped MCP
proxies for client-declared tools. The proxy returns an OpenAI tool call to
the connected harness; it never executes that call. Like the other CLI
adapters, Kilo is local/non-ZDR. Production explicitly includes
`kilo-cli/kilo/kilo-auto/free` in the code pool. For streaming requests, the
adapter reads JSON events as they arrive and forwards assistant text deltas
as OpenAI SSE; lifecycle and diagnostic events are filtered out. Tool calls
continue to be returned as normal OpenAI tool-call deltas. The gateway
heartbeat stays active while waiting for the first CLI text event.

### Warm worker servers

The worker keeps two long-lived `kilo serve` processes so request handling does
not pay Kilo's provider and session startup on every call:

- a **text** server started without an MCP bridge, used for requests that carry
  no tools;
- a **tool** server started with one shared MCP bridge that resolves each
  request's tools through a broker pointer file.

Kilo lists MCP tools per session, but its MCP configuration is fixed at server
startup, so the shared bridge reads `<broker>/active.json` for the in-flight
request's manifest and call channel. Warm tool requests are therefore
serialized, and each request enables its own tools through the session `tools`
map using Kilo's fully-qualified tool ids (`gateway_<manifest mcp name>`).
Request manifests beyond the pre-authorized id range (`_WARM_TOOL_INDEX_LIMIT`,
128) skip the warm path. Sessions are aborted and deleted after every request,
so the server's session store stays bounded.

Every warm failure - a refused turn, an invalid session, a timeout, or a server
startup error - degrades to the isolated per-request `kilo run` path, so tool
correctness never depends on the warm path. `TUSKER_KILO_WARM_ENABLED=0`
disables both servers, and the worker's `/healthz` reports `warm_text` and
`warm_tools`.

In Kubernetes, the gateway forwards Kilo requests to the dedicated
`tusker-kilo-worker` service, pinned to node `visor` and isolated by a
NetworkPolicy that permits ingress only from the gateway pod. The worker has
bounded CPU/memory, receives only the Groq key, and currently allows
`groq/openai/gpt-oss-20b`, `groq/qwen/qwen3.8-27b`, and
`kilo/kilo-auto/free`. The free auto-router passed a live tool-call probe
through the worker HTTP endpoint with a clean home directory and no API keys.
Request-shaped worker failures (for example a 400 *unsupported_message_content*
for an image payload) are relayed with their original status and OpenAI error
code, so the modality probe records an authoritative `unsupported` verdict and
clients see a clean 400 rather than a misleading 502. Worker-side outages and
unparseable bodies still surface as 502 `kilo_worker_failed`. Streaming
requests that reject before the first SSE byte get the same code/message via
the SSE error frame. Kilo has no catalog advertisement for image input, so
re-probing it requires `python -m tusker_gateway.modality_qualification
--include-unadvertised`. Other environments can run the adapter
locally by leaving `TUSKER_KILO_WORKER_URL` unset. The gateway image includes
the pinned Kilo CLI (currently 7.7.9); set `TUSKER_KILO_CLI_PATH` to use an
operator-managed installation instead.

Kilo Auto Free may route to providers that retain or use prompts for
improvement, so it must not receive confidential data or enter the privacy
pool. Big Pickle is likewise not privacy eligible during its free period. The
worker is deliberately not a general-purpose proxy: its model allowlist is
separate from the main gateway's pool configuration.
