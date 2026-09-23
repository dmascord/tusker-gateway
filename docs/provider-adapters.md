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
any default model pool.

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
rejected rather than silently dropped. Streaming requests currently receive
OpenAI SSE after the CLI has completed; gateway heartbeat behavior remains
available while the process runs. CLI token deltas are not yet streamed
incrementally.

The CLI is not bundled in the gateway container and this change does not
install it, provision a subscription, or deploy the adapter. Operators must
assess Anthropic account/CLI terms and runtime credential handling before
enabling it. Without the environment opt-in, requests to this provider are
rejected as disabled.

## OpenCode CLI

The `opencode-cli` adapter runs `opencode run --standalone --format json` and
uses OpenCode Zen model IDs (`opencode/<model>`); a short model name such as
`big-pickle` is expanded to `opencode/big-pickle`. Set
`TUSKER_OPENCODE_CLI_ENABLED=true` to enable it. The executable defaults to
`opencode` on `PATH`, with `TUSKER_OPENCODE_CLI_PATH` and
`TUSKER_OPENCODE_CLI_TIMEOUT_SECS` available for operator overrides. The CLI
uses its existing OpenCode login by default. An explicit `OPENCODE_API_KEY`
is forwarded to the child; the gateway's `OPENCODE_ZEN_API_KEY` is not aliased
to it because those credentials have different sources and may have different
service-policy eligibility.

Each request runs from a private temporary working directory with project
configuration and default plugins disabled. Text-only requests do not inject
custom OpenCode config. Tool requests use MCP-only inline config with
request-scoped gateway proxies; the proxy returns calls to the connected
harness and never executes them. Do not add custom OpenCode `permission`
rules here: testing showed that they make OpenCode Zen return HTTP 403
("free tier can only be used from within OpenCode"). The CLI's own
non-interactive permission handling remains in effect for other tools, so
operators should also review any global CLI configuration used by the gateway.
Follow-up tool calls and results are replayed as transcript history. The CLI
is not bundled in the gateway container; this adapter is disabled by default
and excluded from default pools.

Set `TUSKER_OPENCODE_CLI_PATH` when multiple OpenCode installations exist; the
gateway does not install or upgrade the CLI. The local request probe used
`/Users/tusker/.opencode/bin/opencode`, which reports `v2.0.15`; a separate
Homebrew install reports `1.18.32` and was not used. OpenCode Zen may still
reject CLI/API use based on account or service policy; the adapter does not
bypass those restrictions.

## Kilo Code CLI

The `kilo-cli` adapter runs `kilo run --format json --model provider/model`.
It accepts explicit `provider/model` IDs (for example,
`kilo-cli/anthropic/claude-sonnet-4.6`) and is opt-in with
`TUSKER_KILO_CLI_ENABLED=true`. `TUSKER_KILO_CLI_PATH` chooses the executable
and `TUSKER_KILO_CLI_TIMEOUT_SECS` sets the request timeout (default 600
seconds). The adapter uses Kilo's existing runtime auth; if `KILO_API_KEY` is
set, it is passed only to the child process.

Each request receives high-priority inline `KILO_CONFIG_CONTENT`, disables
project config, denies tools by default, and exposes only request-scoped MCP
proxies for client-declared tools. The proxy returns an OpenAI tool call to
the connected harness; it never executes that call. Like the other CLI
adapters, Kilo is local/non-ZDR, excluded from default pools, not bundled in
the gateway image, and streaming is returned after the CLI completes rather
than as incremental token deltas.
