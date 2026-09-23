# Qwen3.8-27B end-to-end benchmark — 2026-09-23

Additive to `docs/provider-review-2026-09-22.md` (lines 130–131 cover
mlx-mac reachability; this doc drills into the qwen3.8-27b route
specifically and adds direct-vs-gateway latencies).

## TL;DR

- **mlx-mac/qwen3.8-27b is live and serving through the public
  gateway** for both text and image inputs. End-to-end latency
  6.6 s (text) / 9.4 s (vision) on the M4 Max.
- **Direct Ollama (Q4_K_XL, mmproj-F16)**: 20.7 tok/s decode,
  408 tok/s warm prefill — adequate for the privacy pool.
- **Direct MLX-VLM (mlx-community 4-bit)**: 28.5 tok/s decode,
  126 tok/s prefill — faster decode, slower prefill; needs a separate
  durable service to slot behind a gateway provider entry.
- **Operational finding**: the public gateway returns Cloudflare
  `error 1010` to the Python `urllib` User-Agent on large base64
  payloads. Set `User-Agent: curl/8.7.1` (or any SDK UA) for probes.
- **Tool support remains negative** as documented 2026-09-22
  (qualification run failed `no_tool_call`); text+vision are in-scope,
  tools remain gated by `require_tool_qualification`.

## Test apparatus

- Image: programmatically generated 700×520 PNG — three shapes with
  letters (A, B, C), exactly 7 black dots, serial text `47-BRAVO-9`
  for OCR, leftmost shape a red circle.
- Expected answer: shape letters left→right, dot count, leftmost
  color, serial text. Both backends returned the expected answer
  under a concise no-reasoning prompt.

## Direct Ollama (port 11434)

`qwen3.8-27b:latest` — Qwen3_5ForConditionalGeneration, 27.3 B params,
context 262 144, Q4_K_M, projector clip 460.73 M / dims 5120
(vision capable).

| Run | load | prefill | decode | tok/s (decode) |
|---|---:|---:|---:|---:|
| cold | 1.77 s | 0.45 s | 7.67 s | 20.7 |
| warm | 0.15 s | 0.14 s | 7.68 s | 20.7 |

Prefill warm: 408 tok/s. Vision (direct): correct on all four items.

## Direct MLX-VLM (port 11435)

Model `/Users/tusker/models/Qwen3.8-27B-MLX-4bit`
(`mlx-community/Qwen3.8-27B-4bit`, 3 shards ≈ 16 GB), served with
`mlx_vlm.server --enable-thinking`.

Text: 109 tokens in 3.82 s decode → **28.5 tok/s**;
prefill 126 tok/s; peak memory 17.3 GB.
Vision (direct): correct on all four items
(`A, B, C; 7; red; SERIAL 47-BRAVO-9`), 143 tokens, 6.98 s.

Caveat: with `--enable-thinking`, a tight `max_tokens` budget is
consumed by the reasoning channel first — with `max_tokens: 120` both
servers returned empty `content` and non-empty `reasoning`. Probes
should either raise the cap or set a low `reasoning_effort`.

## Through the public gateway

| Route | Latency | Result |
|---|---:|---|
| `mlx-mac/qwen3.8-27b` text | 6.56 s | `GATEWAY_MLX_E2E_OK`, 79 completion tokens |
| `mlx-mac/qwen3.8-27b` vision (base64 PNG) | 9.43 s | `A B C; 7; red; 47-BRAVO-9`, 142 tokens |

Both went through Cloudflare → gateway auth → privacy-pool eligibility
→ mlx-mac passthrough → Ollama. The gateway forwards
`image_url`/data-URI image parts correctly and passes the reasoning
channel through to the client.

### Cloudflare 1010 on non-browser User-Agents

`POST /v1/chat/completions` with a ~30 KB base64 image body from
Python `urllib` (UA `Python-urllib/3.14`) is rejected at the edge with
HTTP 403 `error code: 1010` before reaching the gateway. The same
body with `User-Agent: curl/8.7.1` succeeds. Anyone scripting probes
against `ai.tusker.net.au` must set a recognised UA.

## MLX-VLM durability status

**Decision (2026-09-23): Ollama is the production path; MLX-VLM is not
deployed.** The 28.5 tok/s decode advantage does not justify a second
17 GB copy of the same weights, a second provider entry, and a second
supervised service — particularly given MLX-VLM's 3× slower prefill
(126 vs 408 tok/s) on long prompts.

Cleanup performed on the host:

- stopped the ad-hoc `mlx_vlm.server` on port 11435 (freed ~17 GB;
  system memory 94% free afterwards);
- removed the duplicate Ollama tag `qwen38-vtest2` (same blob as
  `qwen3.8-27b:latest`);
- deleted the stale `~/Library/LaunchAgents/net.tusker.mlx-27b.plist.bak`,
  which launched `mlx_lm.server` and could not have served this VLM.

The weights remain on disk at
`/Users/tusker/models/Qwen3.8-27B-MLX-4bit`, so later promotion is a
service + provider-entry change, not a re-download. If promoted, use a
fresh LaunchAgent running `mlx_vlm.server` with `KeepAlive` and register
a distinct `mlx-vlm-mac` provider entry — do not repoint `mlx-mac`.
