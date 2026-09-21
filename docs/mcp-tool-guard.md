# MCP tool guard

The gateway exposes `POST /mcp` for clients that support Model Context
Protocol elicitation. It provides a small approval broker rather than
executing tools itself.

The advertised `tusker.guard_tool` takes:

```json
{
  "tool_name": "bash",
  "tool_arguments": {"command": "kubectl delete pod api-1"}
}
```

Safe calls return a structured `decision: allow`. High-impact calls return an
MCP `input_required` result containing a boolean approval elicitation and a
short-lived, HMAC-signed `requestState`. The client presents the question to
the user and retries the call with the unchanged tool name and arguments plus:

```json
{
  "requestState": "<opaque state>",
  "inputResponses": {
    "approval": {
      "action": "accept",
      "content": {"approved": true}
    }
  }
}
```

Approval is bound to the exact tool arguments, expires after five minutes, and
is single-use. A changed tool call, invalid signature, malformed response, or
replayed state cannot authorize execution. The endpoint only authorizes; the
client remains responsible for executing the original tool.

OMP/OpenCode clients use their native built-in `question` tool for action-
capable streaming and complete responses. The gateway replaces the risky
provider call with a question tool call, then validates the returned answer
against the exact pending call before allowing the next model turn to proceed.
Other OpenAI-compatible clients continue to receive the existing
`approval_required` response unless they implement the same question tool
contract. MCP-aware clients can instead call `tusker.guard_tool` directly.
