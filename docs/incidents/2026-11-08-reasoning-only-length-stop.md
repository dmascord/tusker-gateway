# Reasoning-only `length` turns start an unwinnable client retry loop (2026-11-07, 2026-11-08)

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
    and not inband_thinking.has_visible_text
    and not saw_tool_markup
):
    emitted_finish_reason = "length"
    raise unusable_tool_error("reasoning_only_or_empty")
```

The `inband_thinking` test replaced the original `not saw_visible_content`
test on 2026-11-08; see the recurrence section below for why.

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

## Recurrence: MiniMax-M3 leaks its thinking into the content channel

The same client error came back on 2026-11-08. The failing turn was
`minimax/MiniMax-M3` at 05:46:59Z, and its usage frame read:

```json
"completion_tokens": 16384,
"completion_tokens_details": {"reasoning_tokens": 16384}
```

The whole 16384-token budget went to reasoning and no answer was produced.
The budget floor from the first fix does not apply here: the client had
already asked for 16384 tokens.

The turn reached the `length` guard with `saw_visible_content` **true**, so
the guard let it through. MiniMax-M3 does not use the `reasoning_content`
channel: it streams its chain of thought in the OpenAI `content` channel,
wrapped in `<think>`. The gateway saw "content" and concluded the client had
something to show.

It did not. OMP heals the leak: a `<think>` block in the content channel is
moved into a thinking block, and OMP's `assistantTurnDelivered` counts only
text, tool calls, and images. A turn holding nothing but a thinking block is
not delivered, so the client retried the identical request three times and
surrendered with the same message.

The gateway was therefore measuring the wrong thing. "Content was seen" is
not "the client has something to show" once the provider leaks its thinking
into the content channel.

### `tusker_gateway/inband_thinking.py` — classify the content the client will render

New module. `InbandThinkingTracker` mirrors the tag vocabulary of OMP's
`ThinkingInbandScanner` (`<think>`, `<thinking>`, `<scratchpad>`, the
`` ```thinking `` fence) and tracks, across stream frames, how much of the
content channel the client will render as ordinary text:

- an opener starts a thinking section; the text inside it is reasoning;
- a bare closer with no opener means the provider template prefilled the
  opener into the prompt (DeepSeek-R1, Qwen3-Thinking), so the text in front
  of it was reasoning;
- a bare closer that arrives next to native reasoning or a tool call is a
  stray, because OMP only reclassifies a bare closer when the message holds
  nothing but that one text block;
- text after a closer is the answer.

The tracker never changes the bytes the gateway forwards. It only decides
whether the client is left with something it can show.

The length guard now uses it:

```python
if (
    fr == "length"
    and not saw_tool_call
    and not has_tools
    and not pending_tool_frames
    and not inband_thinking.has_visible_text
    and not saw_tool_markup
):
    emitted_finish_reason = "length"
    raise unusable_tool_error("reasoning_only_or_empty")
```

The guard also runs *before* the generic empty-stream guard, so the client
gets the actionable "output budget exhausted" error rather than a bare
"empty response".

The classification is fed **before** the redaction restorer, not after it.
The restorer holds a trailing run of text back until the stream ends, so a
short answer ("42") can reach the client only in the tail frame; classifying
the restored bytes would miss it.

### Non-streaming path

`_response_has_visible_content` applies the same test to a complete
response, so a non-streaming turn that holds only leaked reasoning is
rejected the same way.

### Verification

- `tests/test_inband_thinking.py` — 16 unit tests over the tracker's tag,
  split-frame, implied-close, stray-close, and native-reasoning rules.
- `tests/test_tool_stream_integrity.py` — the MiniMax shapes end to end
  through the public handler: thinking-only turn rejected, bare close
  followed by an answer delivered, one-word answer delivered, thinking tag
  split across frames rejected, native reasoning keeps the text visible.
- `tests/test_tool_stream_integrity.py::test_tools_sent_thinking_only_length_turn_is_rejected`
  covers the tools-enabled path, which is the shape the failing
  `hermes-code` route actually sends (13 tools). The length guard tests
  `has_tools`, which is frame-level (`delta.tool_calls`), so a
  tools-requested turn that never produced a tool call still has
  `has_tools=False` and reaches the guard. Reverting the tracker to
  `saw_visible_content` fails this test.
- Reverting the tracker to `saw_visible_content` fails the MiniMax tests
  as well.
- Full offline suite: 2125 passed, 8 skipped.

### Production evidence

The guard was confirmed live against the deployed image
(`sha256:b2252f0a870f386910d8391482cea73555ae09c8abf56ae7bd0c81699e8ad7d4`,
commit `431296e`), on real MiniMax-M3 traffic:

```
unusable tool response rejected provider=minimax model=MiniMax-M3
  request_id=req_095bfc8b273e7810 reason=output_budget_exhausted
  reasoning_chars=0 content_chars=7380 chunks=831
```

`reasoning_chars=0` with `content_chars=7380` is the leak in one line:
MiniMax put its whole chain of thought in the OpenAI `content` channel.
The gateway rejected the turn instead of forwarding the terminal.

The client-visible result for the same shape is a `stop` terminal carrying
an actionable message, never `length`:

```
data: {"delta": {"content": "The provider minimax/MiniMax-M3 spent its whole
  output budget on reasoning without producing an answer. The output budget
  was exhausted: ... Request ID: req_a52677d72ffb18d4."}}
data: {"delta": {}, "finish_reason": "stop"}
data: [DONE]
```

OMP's length-stop recovery never starts, because the turn carries text and
the terminal is `stop`.

## Scope notes

The Codex path was checked and ruled out. `_chat_codex` deliberately drops
`max_tokens`, `max_completion_tokens` and `max_output_tokens` because the
Codex Responses backend rejects them outright (see
`tests/test_passthrough_providers.py::test_chat_codex_drops_max_tokens_params`).
Codex candidates therefore use their own defaults and cannot hit this
failure mode.
