# Project TODOs


## Reasoning-only `length` turns (2026-11-07, 2026-11-08)

Fixed, twice. A reasoning-heavy candidate spent the client's whole 120-token output
budget on reasoning and stopped with `finish_reason=length` and no visible
text. The gateway forwarded that dead terminal, and OMP retried the
identical request three times before raising "Length-stop recovery gave up".

The gateway now rejects a `length` terminal that carries no visible output
or tool call, so the client receives an actionable error instead of a
terminal it cannot recover from, and the reasoning-model floor now covers the
DeepSeek V4 family and five other models confirmed in production logs to
stream a reasoning channel.

The same error recurred on 2026-11-08 against `minimax/MiniMax-M3`, which
streams its chain of thought in the OpenAI `content` channel instead of
`reasoning_content`. The gateway saw "content" and concluded the client had
something to show; OMP moved the leaked thinking into a thinking block and
found nothing deliverable. `tusker_gateway/inband_thinking.py` now classifies
the content the client will actually render, using the same tag rules as OMP.
