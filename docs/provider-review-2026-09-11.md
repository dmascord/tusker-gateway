# Provider Performance Review — 2026-09-11

## Executive Summary

**Codex accounts (3): justified, but openai-codex OAuth is broken.**

The 3 codex accounts (openai-codex) are the backbone of the gateway — 3,813 calls with 100% quality for gpt-5.6-luna. However, all 3 accounts show `refresh_token_reused` (401) errors on rotation, meaning OAuth is broken for the openai-codex provider. The actual traffic is going through github-copilot-enterprise instead (2,691 calls, 100% quality).

## Provider Performance (7-day)

### Tier 1: Excellent (< 5% error rate)

| Provider | Requests | Failures | Error Rate | Status |
|---|---|---|---|---|
| **xiaomi** | 13,370 | 22 | 0.2% | ✅ Dominant, excellent |
| **minimax** | 4,807 | 29 | 0.6% | ✅ Excellent |
| **zai** | 278 | 2 | 0.7% | ✅ Excellent |
| **opencode-zen** | 2,254 | 47 | 2.1% | ✅ Good |
| **openrouter** | 1,308 | 41 | 3.1% | ✅ Good |

### Tier 2: Moderate (5-25% error rate)

| Provider | Requests | Failures | Error Rate | Status |
|---|---|---|---|---|
| **ollama-cloud** | 4,701 | 506 | 10.8% | ⚠️ Moderate, quota issues |
| **local-llm** | 41 | 6 | 14.6% | ⚠️ Slow but working |
| **synthetic** | 2,513 | 467 | 18.6% | ⚠️ Quota-exhausted frequently |
| **alibaba** | 203 | 45 | 22.2% | ⚠️ Mixed performance |

### Tier 3: Broken (> 25% error rate)

| Provider | Requests | Failures | Error Rate | Issue |
|---|---|---|---|---|
| **opencode-go** | 166 | 115 | 69.3% | Mixed quota issues |
| **mlx-mac** | 53 | 37 | 69.8% | Timeout issues |
| **google** | 291 | 189 | 64.9% | API key expired/unconfigured |
| **github-copilot-enterprise** | 144 | 101 | 70.1% | OAuth issues for some models |
| **groq** | 254 | 221 | 87.0% | API key invalid/quota |
| **workers-ai** | 721 | 643 | 89.2% | Free tier exhausted daily |
| **cerebras** | 38 | 38 | 100% | API key invalid/quota |
| **nvidia** | 13 | 13 | 100% | API key invalid/quota |
| **github-copilot** | 6 | 6 | 100% | OAuth broken |

## Codex Account Assessment

### openai-codex (3 accounts rotating)
- **Quality**: 100% (3,813 calls, 0 failures)
- **OAuth status**: ALL 3 accounts show `refresh_token_reused` (401) on rotation
- **Impact**: Catalog fetch still works (cached?), but chat requests may fail
- **Verdict**: Keep accounts — they're the highest-quality provider, but fix OAuth rotation

### github-copilot-enterprise (1 account)
- **Quality**: 100% for gpt-5.6-luna (2,691 calls), 100% for gpt-5.4-mini (360 calls)
- **OAuth status**: Working correctly
- **Impact**: Currently serving most traffic as fallback for openai-codex
- **Verdict**: Keep — critical backup provider

### github-copilot (1 account)
- **Quality**: 14% (6 calls, 6 failures)
- **OAuth status**: Broken (100% error rate)
- **Impact**: Zero useful traffic
- **Verdict**: Remove or fix — dead weight

## Privacy Pool 502 Root Cause

The user's hypothesis was **correct**: Jetson and MLX Mac ARE in the privacy pool.

**Evidence** (request `req_77275095adfb4b37`):
```
Attempt 1/6: workers-ai/@cf/meta/llama-4-scout-17b-16e-instruct → 429 (free tier exhausted)
Attempt 2/6: synthetic/syn:small:text → 429 (subscription rate limit)
Attempt 3/6: ollama-cloud/glm-5.1 → 429 (weekly limit)
Attempt 4/6: local-llm/llama3.2:3b → timeout (30s)
Attempt 5/6: mlx-mac/mlx-community/Qwen3-8B-4bit → timeout (30s)
Attempt 6/6: local-llm/qwen2.5-coder:7b → timeout (30s)
→ 502 "provider attempt idle timeout" after 92s
```

**Root cause**: The 30s per-candidate timeout is too aggressive for local backends. When all free upstreams are exhausted AND both local backends time out, the pool returns 502.

**Local backend status**:
- Jetson (10.0.0.212): reachable, responding to `/api/tags`, but slow for inference
- MLX Mac (10.0.0.141): reachable, responding to `/api/tags`, but slow for inference

## Recommendations

1. **Keep 3 codex accounts** — they're the backbone (6,504 calls, 100% quality combined)
2. **Fix openai-codex OAuth** — all 3 accounts showing `refresh_token_reused` errors
3. **Remove dead providers** — cerebras, nvidia, workers-ai, google, groq are >60% error rate
4. **Increase local backend timeout** — 30s is too aggressive for Jetson/MLX Mac; consider 60-90s
5. **Monitor ollama-cloud** — 10.8% error rate is moderate; may need quota increase

## Privacy Pool Fix Options

**Option A: Increase timeout** (recommended)
- Change per-candidate timeout from 30s to 60s for local backends
- Tradeoff: Longer request latency when backends are truly down

**Option B: Remove local backends from fallback chain**
- They're already excluded from `auto_catalog_providers`
- But they're still in the pool as manual entries
- Tradeoff: Lose privacy-guaranteed inference

**Option C: Add health checks**
- Pre-filter candidates based on recent success rate
- Tradeoff: More complex, may miss transient issues
