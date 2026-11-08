# Reasoning-only `length` turns start an unwinnable client retry loop (2026-11-07)

Client symptom:

```
Error: compaction: Length-stop recovery gave up after 3 consecutive `length`
responses from tusker-gateway/hermes-code with no text or tool call.
Try switching models or raising the model's max output tokens.
```

The gateway forwarded a `finish_reason=length` terminal for a turn that
contained **zero visible output**. The client treats such a turn as "nothing
delivered", retries the *identical* request, and receives the same `length`
terminal every time. The retry loop cannot succeed by construction, so it
ends in a client-side surrender rather than an actionable error.

## Root cause

Two independent defects combined.

### 1. The gateway handed a dead `length` terminal to the client

`_normalize_stream` (`tusker_gateway/endpoints.py`) checked the terminal
frame for empty turns but let `finish_reason=length` through when the model
had streamed reasoning and no visible content. The `length` terminal is the
one terminal the client cannot recover from on its own: it neither delivers a
message nor signals a retryable condition, so OMP's length-stop recovery
re-issues the same request and hits the same wall.

The gateway already had the right concept elsewhere — an unusable turn is
rejected before the terminal is yielded so the client sees an actionable
error — but the `length` terminal was never routed into it.

### 2. The client's output budget was too small for reasoning models

The production turn captured upstream carries the smoking gun in its usage
frame:

```json
"completion_tokens": 120,
"completion_tokens_details": {"reasoning_tokens": 120}
```

The client's cap was **120 tokens**, and the model spent **all 120** on
reasoning (`reasoning_tokens: 120`), leaving `content_chars=0`. The upstream
never had room to emit an answer, so no amount of retrying would have
produced one.

`apply_max_tokens_floor` (`tusker_gateway/max_tokens.py`) already floors the
output budget for known reasoning models, but the DeepSeek V4 family was not
in its slug list, and neither were five other models that were later
confirmed in production logs to stream a reasoning channel.

## Evidence

Captured upstream bytes: `tests/fixtures/reasoning_only_length_turn.sse`,
byte-identical to production capture `c16bddd22ee54ceb926fcad161a10a9a`
(provider `synthetic/hf:deepseek-ai/DeepSeek-V4.1-Flash-1017`, 2026-11-07).

The stream is 24 reasoning deltas and nothing else:

```
data: {... "delta":{"reasoning":"We"}, "finish_reason":null ...}
...
data: {... "delta":{"reasoning":" Could be \"success\" or"}, "finish_reason":"length" ...}
data: {... "choices":[], "usage":{... "completion_tokens":120,
       "completion_tokens_details":{"reasoning_tokens":120}} ...}
data: [DONE]
```

No frame carries a `content` delta and no frame carries a `tool_calls`
delta. The capture is stored with verdict `empty_stream`.

## Fixes

### `tusker_gateway/endpoints.py` — reject the dead terminal

A terminal `finish_reason=length` with no tool call, no visible content and
no tool markup is now rejected **before** the terminal frame is yielded:

```python
if (
    fr == "length"
    and not saw_tool_call
    and not has_tools
    and not pending_tool_frames
    and not saw_visible_content
    and not saw_tool_markup
):
    emitted_finish_reason = "length"
    raise unusable_tool_error("reasoning_only_or_empty")
```

Setting `emitted_finish_reason = "length"` before raising makes
`unusable_tool_error` reclassify the failure as `output_budget_exhausted`:
the route ran out of room, which is a property of the request, not evidence
that the model breaks the tool contract. The client receives an actionable
error instead of a terminal it will retry forever.

### `tusker_gateway/max_tokens.py` — floor the budget for reasoning models

The reasoning slug list gained the models confirmed to stream a reasoning
channel in production gateway logs (`fields=reasoning_content`):

- `deepseek-v4` — the DeepSeek V4 family, including
  `hf:deepseek-ai/DeepSeek-V4.1-Flash-1017` from this incident
- `kimi-k3`, `claude-fable`, `ornith`, `gemma4`, `beam`

With the floor in place the 120-token client cap is lifted to 2000 tokens for
these routes, so the model has room to answer after thinking.

### `tusker_gateway/endpoints.py` — accurate error text

The `output_budget_exhausted` message previously claimed "the prompt nearly
filled its context window", which is false for this incident — the prompt was
6462 tokens against a 128k window; the *output* cap was the problem. The
message now names the output budget as the exhausted resource and offers
both remedies (raise the output budget, or compact the conversation).

## Verification

- `tests/test_tool_stream_integrity.py::test_reasoning_only_length_turn_never_reaches_the_client_as_length`
  replays the real captured bytes through the public handler and asserts the
  client sees `finish_reason=stop` with the actionable message, never
  `length`.
- Reverting only the `endpoints.py` guard fails that test.
- Reverting only the `max_tokens.py` floor fails that test and the two
  reasoning-model tests. Both changes are independently required.
- Full offline suite: 2100 passed, 8 skipped.

## Scope notes

The Codex path was checked and ruled out. `_chat_codex` deliberately drops
`max_tokens`, `max_completion_tokens` and `max_output_tokens` because the
Codex Responses backend rejects them outright (see
`tests/test_passthrough_providers.py::test_chat_codex_drops_max_tokens_params`).
Codex candidates therefore use their own defaults and cannot hit this
failure mode.
