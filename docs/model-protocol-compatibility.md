# Provider/model protocol compatibility

## Contract exposed to clients

The gateway exposes OpenAI-compatible Chat Completions to OMP, OpenCode,
Cline and other clients. Provider-specific model families are **not** a
reliable indication of an upstream wire dialect. Keep these boundaries:

- Ordinary answer text: `message.content` / `delta.content`.
- Thinking text: `reasoning_content` / `delta.reasoning_content`.
- Executable calls: `tool_calls` with stable IDs, function names and JSON
  argument strings. Do not interpret thinking text as executable output.
- Tool results: `role=tool` with `tool_call_id` for Chat Completions;
  `function_call_output` with `call_id` for Responses.
- Streaming: finish_reason is a separate terminal event; native structured
  tool calls win over duplicate text markup.
- A tool result is **not** proof that execution succeeded.

## Compatibility resolution

1. Resolve the actual upstream provider/model **for each candidate attempt**.
2. Use provider-scoped native adaptations, never model-name guesses alone.
3. Consult fresh, endpoint-scoped evidence for optional features. Stale,
   unknown and temporarily unavailable evidence must not be treated as
   unsupported or passed.
4. Apply explicit operator overrides only after validating the endpoint
   identity and documenting the reason.
5. Preserve replay-sensitive reasoning and provider signatures where
   required; do not invent signatures or reconstruct encrypted thinking.
6. On failover, build a new upstream body from the original messages rather
   than carrying another provider's mutations forward.

## Probe matrix

The following checks require actual upstream execution. The pure classifiers
in `protocol_probe.py` only evaluate responses; they **do not run probes**.

| Feature | Positive proof | Failure classification |
| --- | --- | --- |
| structured_tool_calls | Nonempty native calls with IDs and parseable JSON args | Malformed/absent -> unknown |
| tool_call_finish | Valid calls and `finish_reason=tool_calls` | `stop` -> unknown (client may compensate) |
| reasoning_channel | Separate nonempty `reasoning_content` | Absent -> unknown |
| reasoning_replay | Successful second turn replaying exact required thinking | One-turn probe -> unknown |
| reasoning_effort | Controlled request accepted with intended effort | 4xx requires structured error review |
| thinking_parameter | Native provider accepts documented parameter | 4xx requires structured error review |
| streaming_tool_fragments | Assembled stream with valid ID, name, JSON arguments and terminal | Incomplete -> unknown |
| responses_function_calls | Responses `function_call` followed by matching output | Chat-only probe -> unknown |

Never store prompts, arguments, reasoning, tool outputs or credentials in
protocol evidence. Version probes when semantics change. Use distinct endpoint
IDs for deployments that share provider and model names.

## Provider families to cover with recorded fixtures

- DeepSeek R1/V3/V4: native vs routed, DSML, thinking replay, fragmented calls.
- GLM / Z.AI: native thinking controls vs routed OpenAI-style settings.
- Qwen: Hermes/Qwen function markup, inline think blocks, tool finish.
- Kimi / Moonshot: reasoning channel and replay requirements by endpoint.
- MiniMax / MiMo: inline thinking and native structured calls.
- OpenAI/APIM/Codex: effort, completion tokens, Responses tool outputs.
- Anthropic/LiteLLM: signed thinking and encrypted replay preservation.
- Gemini: thought signatures on tool continuations.
- Mistral: typed reasoning content parts.

## Current rollout status

This PR introduces conservative profiles, copy-on-write replay adaptation,
a versioned evidence store and pure probe classifiers. It does **not** yet
provide automatic active probing, signed-reasoning translation for every
provider, or proven compatibility across every deployment. Leave it draft
until integration fixtures, test runs and representative provider smoke tests
are complete.
