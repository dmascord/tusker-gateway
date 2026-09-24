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
mode. Images and other multimodal message content remain unsupported and are
rejected rather than silently dropped. Streaming requests use Claude Code's
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

The `opencode-cli` adapter runs `opencode run --standalone --format json` and
uses OpenCode Zen model IDs (`opencode/<model>`); a short model name such as
`big-pickle` is expanded to `opencode/big-pickle`. Set
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
In Kubernetes, the gateway forwards Kilo requests to the dedicated
`tusker-kilo-worker` service, pinned to node `visor` and isolated by a
NetworkPolicy that permits ingress only from the gateway pod. The worker has
bounded CPU/memory, receives only the Groq key, and currently allows
`groq/openai/gpt-oss-20b`, `groq/qwen/qwen3.8-27b`, and
`kilo/kilo-auto/free`. The free auto-router passed a live tool-call probe
through the worker HTTP endpoint with a clean home directory and no API keys.
Other environments can run the adapter
locally by leaving `TUSKER_KILO_WORKER_URL` unset. The gateway image includes
the pinned Kilo CLI (currently 7.7.9); set `TUSKER_KILO_CLI_PATH` to use an
operator-managed installation instead.

Kilo Auto Free may route to providers that retain or use prompts for
improvement, so it must not receive confidential data or enter the privacy
pool. Big Pickle is likewise not privacy eligible during its free period. The
worker is deliberately not a general-purpose proxy: its model allowlist is
separate from the main gateway's pool configuration.
