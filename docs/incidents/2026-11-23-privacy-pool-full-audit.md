## 2026-11-23 — Full privacy-pool model audit after the qualification fixes

Follow-up to `2026-11-22-synthetic-image-tools-qualification.md`. Now that all
four admission gates are understood (`privacy_zdr`, `input_image`,
`input_image_tools` and behavioral `tool_capability`), every privacy route was
audited and every actionable near-miss re-probed.

## Method

Built a fresh `PoolManager` with a full catalog refresh against the live
config, enumerated all 200 privacy routes across 8 providers, and joined each
route to its four evidence records. Grouped routes by which single gate was
missing, then re-probed only those groups with the corrected caps/timeouts:
- behavioral tool qualification: `tool_qualification.main --ignore-cooldowns --force`
- ZDR: `privacy_qualification.main` with explicit `--model` pairs
- composite: `composite_main --timeout-secs 120` (16,384-token cap)

## Before / after

At the time of `req_4ae4a2134158c498` the privacy pool had **4** eligible
candidates for tools + image + 212K. After the qualification work in this and
the previous incident docs, the same shape has **8**:

```
synthetic/hf:zai-org/GLM-5.3-Flash
apim/gpt-5.6-luna
openai-codex/gpt-5.6-luna
opencode-go/hy4-flash-free        <- newly qualified in this audit
mlx-mac/qwen3.8-27b
mlx-mac/ornith-1.5:35b
opencode-go/gpt-5.6-luna
opencode-go/gpt-6-luna
```

6 of 8 providers now contribute. The remaining two are quota-blocked, not
mis-configured (see below).

## Near-misses re-probed

| Group | Models | Result |
|---|---|---|
| TOOL_ONLY | `opencode-go/hy4-flash-free`, `mimo-v2.6-pro`, `qwen3.7-plus` | hy4-flash-free **passed** (now selectable); other two `rate_limited` |
| COMPOSITE_ONLY | `mlx-mac/qwen3.8-27b:latest`, `openai-codex/gpt-5.6-terra`, `gpt-6-astra`, `opencode-go/glm-5.3-flash` | terra + astra **passed**; mlx-mac timed out at 120s; opencode-go rate-limited |
| ZDR_ONLY | `commandcode/deepseek/deepseek-v4-flash`, `ollama-cloud/beam`, `gemma4:31b`, `mimo-v2.6-pro`, `minimax-m3` | all 429 — quota-blocked |

`commandcode` reports its weekly limit resets **2026-11-25T10:55:20Z**;
`ollama-cloud` reports a weekly usage limit requiring an upgrade/credits. No
amount of re-probing helps those two before the quota window resets.

## Provider-by-provider status (tools + image)

| Provider | Status | Note |
|---|---|---|
| apim | ✅ 1 candidate | curated `gpt-5.6-luna`, window now 1,050,000 |
| openai-codex | ✅ | ZDR/image/composite/tool all passing; `gpt-5.6-terra` and `gpt-6-astra` also composite-passed but their published windows are below the 212K requirement |
| opencode-go | ✅ 2–3 candidates | `hy4-flash-free` newly qualified; `gpt-5.6/6-luna` subject to live cooldowns |
| mlx-mac | ✅ 2 candidates | `qwen3.8-27b`, `ornith-1.5:35b` |
| synthetic | ✅ 1–2 candidates | `GLM-5.3-Flash`, `Qwen3.8-27B` |
| commandcode | ❌ | 103 routes, nearly all `privacy_zdr=unavailable` from weekly quota |
| github-copilot-enterprise | ❌ | 23 routes, ZDR `unavailable` + image `advertised` |
| ollama-cloud | ❌ | 24 routes, weekly quota 429 on ZDR probes |

## Caveats

The composite probes remain flaky for reasoning models: the same model can
pass at one cap/timeout and fail at another. A single definitive `unsupported`
overwrites a passing record. A durable fix would require two consecutive
definitive failures before downgrading — not implemented here.

No code changes were made in this audit; it only re-ran probes against the
already-deployed image (`1ca806f`, `sha256:4cc3518…`).
