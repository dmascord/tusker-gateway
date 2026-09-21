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

OpenAI-compatible `/v1/chat/completions` clients continue to use the existing
gateway guard and receive the existing `approval_required` response because
that protocol has no standard mid-request elicitation envelope. An MCP-aware
client should call `tusker.guard_tool` before executing a consequential tool.
