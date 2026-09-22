# Interactive question adapters

The gateway's approval state is client-neutral, but interactive question
tools are not standardized across agent harnesses. The chat endpoint selects a
client adapter using `X-Tusker-Harness` (preferred), `X-Client-Harness`, a
`harness`/`metadata.harness` request field, and finally a conservative
User-Agent hint.

Supported profiles:

| Profile | Emitted tool | Envelope |
| --- | --- | --- |
| `omp` | `ask` | OMP `questions[]` |
| `opencode` | `question` | OpenCode questions/options |
| `cline` | `ask_question` | Cline question/follow-up options |
| `roo` | `ask_followup_question` | Roo/Cline follow-up options |
| `continue` | `AskQuestion` | Continue question/options |
| `cursor`, `claude_code`, `codex_cli`, `gemini_cli`, `aider` | `ask` fallback | Host-permission clients remain fail-closed |

For example:

```http
X-Tusker-Harness: cline
```

Unknown clients use the OMP-compatible adapter. This is deliberate: a weak
client hint must not disable the gateway's high-impact guard. Cursor, Claude
Code, Codex CLI, Gemini CLI, and Aider primarily perform approvals in their
host UI or hooks rather than through a common model-visible question tool, so
they currently receive the safe OMP fallback until an explicit host-permission
capability handshake is implemented.

Approval records remain bound to the opaque question call ID and the exact
original tool call. The adapter changes only the presentation envelope; it
does not change risk classification, expiry, audit logging, or direct replay.
