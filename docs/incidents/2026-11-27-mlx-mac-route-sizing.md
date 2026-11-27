# MLX Mac route sizing and the Inkling 2 Mini evaluation (2026-11-27)

## Summary

Five issues were investigated and addressed around the local MLX Mac
(M4 Max, 48 GB) as a privacy-pool backend: an oversized-prompt admission
gap, an unexplained HTTP 400/500 class, a context window that was larger
than the machine could serve, the Inkling 2 Mini tool-call markup, and the
Inkling route itself.

## 1. Context admission: the estimator under-counted

`estimate_prompt_tokens` divides characters by a flat 3.2 chars/token.
Measured against the MLX Mac's own tokenizer on 2026-11-27:

| Content | Gateway estimate | Model actual | Error |
|---|---:|---:|---:|
| Qwen prose | 5,637 | 4,058 | +38.9% (over) |
| Qwen code | 6,256 | 6,456 | −3.1% |
| Qwen JSON | 5,504 | 5,256 | +4.7% |
| Inkling prose | 5,637 | 4,822 | +16.9% (over) |
| Inkling code | 6,256 | 7,622 | **−17.9%** |
| Inkling JSON | 5,504 | 7,622 | **−27.8%** |

The negative errors are the dangerous ones: code and JSON conversations
are exactly what agentic harnesses send, and the gateway admitted them
against a raw estimate that was up to 28% too small.

On a local route an under-count is expensive. Prefill on the MLX Mac runs
at roughly 55-68 tok/s at 200k tokens, so an oversized prompt is not
rejected when it is admitted — it is pre-filled for minutes, past the
gateway's 420 s `mlx-mac` attempt budget, before failing.

**Fix.** `TUSKER_CONTEXT_SAFETY_FACTOR_JSON` widens the required window
per provider or provider/model. `PoolManager.select` multiplies the
required context tokens by the factor before comparing against the route's
declared window, in both the candidate filter and the sticky-route check.
The deployment sets `{"mlx-mac": 1.35}`, which covers the observed
under-count with margin. The factor only ever *reduces* admission, so a
misconfiguration cannot admit more than the raw estimate allowed.

## 2. The unexplained 400/500 class

107 `http_400` captures over 7 days (39 on 2026-11-20 alone) plus 13
`http_500`, concentrated on `mlx-mac`. Reproduced on 2026-11-27 by
probing request shapes against the Ollama endpoint:

| Shape | Result |
|---|---|
| `reasoning_effort: "xhigh"` | **HTTP 500** — the chat template raises `Unexpected reasoning effort` |
| `developer`-role message | **HTTP 500** — the template raises `System message must be at the beginning` |
| `messages: []` | HTTP 400 `[] is too short - 'messages'` |
| tools, tool_choice variants, tool-call replay, reasoning replay, `response_format`, image input, `max_tokens: 200000` | HTTP 200 |

The `xhigh` case was already handled (downgraded to `high` for every
provider except `openai-codex` and `opencode-go`, per
`docs/incidents/2026-11-20-xhigh-reasoning-effort-downgrade.md`). The
`developer` role was not: OpenAI replaced `system` with `developer`, and
the gateway accepts the role but forwarded it verbatim, so Ollama's Go
chat template raised an exception and the turn failed.

**Fix.** `_fold_developer_role` in `tusker_gateway/passthrough.py` rewrites
`developer` messages to `system` for the providers whose templates reject
it (`mlx-mac`, `local-llm`). The instruction is kept rather than dropped.
Empty messages were already rejected at the gateway with a clean 400, so
the upstream 400 was only reachable through a direct-route path.

## 3. Context windows: declared larger than the machine could serve

`qwen3.8-27b` was declared at 262144, the server-wide
`OLLAMA_CONTEXT_LENGTH`. Measured warm on the MLX Mac:

| Model | 32k | 64k | 128k | 256k | VRAM |
|---|---:|---:|---:|---:|---|
| `inkling-2-mini` NVFP4 (MLX) | 67.1 | 81.0 | 78.7 | 60.8 tok/s | flat ~22 GB |
| `qwen3.8-27b` (llama.cpp) | 19.9 | 20.4 | 16.6 | **11.9** tok/s | 17.4 → **27.5 GB** |
| `shirdel-coder-9b` (llama.cpp) | — | 34.5 | 35.8 | — | 7.6 → 9.2 GB |

llama.cpp allocates the whole KV cache eagerly, so a 256k declaration
costs 5 GB of resident memory and 30% of the decode rate. The MLX runner
allocates KV lazily and is nearly flat across the range.

Prefill, measured with a ~9-12k token prompt:

| Model | Prefill rate |
|---|---|
| `qwen3.8-27b` | 194-214 tok/s |
| `inkling-2-mini` NVFP4 | 802-907 tok/s |

**Fix.** `qwen3.8-27b` is declared at 131072 and aliased to a derived
`qwen3.8-27b:128k` tag on the Mac. `shirdel-coder-9b` keeps 262144 (its
7B weights leave headroom). The MLX Mac's server-wide
`OLLAMA_CONTEXT_LENGTH` stays at 262144 because the Ollama `/v1` endpoint
ignores a per-request `num_ctx` — a per-model window requires a derived
tag.

## 4. Inkling 2 Mini: the tool-call markup

Inkling 2 Mini answers a tool request with the call in assistant content
rather than in a structured field:

```
<tusker_capability_probe path="/v1/canary/probe" />
```

The gateway's parser handled `<invoke name=...>`, `<tool_call>{json}`
and `<function=name>` but not this form, so the call stayed as prose and
the behavioral tool-contract probe reported zero tool calls.

**Fix.** `parse_text_tool_calls` gained a branch for the self-closing
form, gated on `allowed_tool_names` — the tools the request actually
asked for. The scope is required: the shape is indistinguishable from
ordinary HTML (`<body class="x">`), and an unscoped branch would promote
HTML to spurious tool calls. The names are threaded from the request
through `normalize_response_tool_calls`, `_normalize_stream`, and the
`_ToolCallStripper`.

## 5. The Inkling route

The native NVFP4 MLX build is materially faster than the IQ4_NL GGUF
build that was measured first, and faster than the curated MLX routes:

| Route | 300-token text | tool turn |
|---|---:|---:|
| `inkling-2-mini` NVFP4 | **5.7 s** | **5.6 s** |
| `qwen3.8-27b` | 16.4 s | 18.0 s |
| `shirdel-coder-9b` | 6.8 s | 5.1 s |

Installing it needs the **full** Ollama build: the Homebrew bottle has no
MLX runtime and rejects the model with `this model requires MLX support,
but the MLX runtime is not available`. The GitHub release tarball
(188 MB, versus the 22.8 MB bottle) carries the Metal kernels.

Inkling was denied on the operator deny-list while it was unqualified,
and the entry was lifted the same day once it passed all three privacy
gates: behavioral tool contract (`stream-tool-contract-v1`, level 4, one
tool call, arguments valid and matching), composite image+tools
(`image-tool-contract-v3`) and privacy ZDR (`privacy-zdr-v1`). It serves
the privacy pool as a curated static route. See
`docs/gateway-model-routing.md` for the deny-list policy.

## 6. Two probe defects the qualification surfaced

Qualifying Inkling exposed two defects in the gateway's own probe, both of
which had produced false negatives for reasoning models.

**The direct-route deny-list had no probe exemption.** A route on
`TUSKER_BLACKLISTED_MODELS` could never be re-probed: the runner that
decides whether the route should come back could not reach it. The
cooldown gate already exempts these probes; the blacklist now does too,
under the same rule (private probe header AND loopback remote, so an
authenticated external client cannot use it to reach a denied route).

**The probe's wording stalled Inkling's reasoning.** The user message
ended with "Do not call any other function." Against Inkling 2 Mini that
phrase deterministically sent the model into a reasoning loop: 66,730
reasoning characters, `finish_reason: length`, zero tool calls at every
token budget tested (192 through 16,384), 10/10 failures. The identical
request without the suffix produced a clean `tool_calls` finish. The
phrase added no contract — `tool_choice: "required"` already forbids
extra calls — and it was the only difference between a passing and a
failing request.

Separately, the probe's 192-token output budget was too small for a
reasoning model, which spends its budget on the preamble before emitting
the call. The composite image+tools probe already defaulted to 16,384 for
exactly this reason; the behavioral probe now does too, tunable with
`TUSKER_TOOL_QUALIFICATION_MAX_TOKENS`.

## Operational notes

- The MLX Mac serves three large models; weights alone total ~45 GB of
  48 GB. Loading several at once exhausts memory and swaps (observed
  2 GB of swap on 2026-11-27). Keep at most two resident.
- `ollama list` on the Mac currently holds `inkling-2-mini:26b-a5b-nvfp4`,
  `inkling-2-mini:mlx32k`, `qwen3.8-27b:128k` and the pre-existing tags.
- The Mac's Ollama was upgraded from 0.30.11 to 0.40.18 to run the Inkling
  model; the previous binary is at `/tmp/ollama-0.30.11.bak` on the Mac.
