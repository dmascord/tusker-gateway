# Provider Strategy Analysis — 2026-09-13

## Problem: 7 providers have working keys but are effectively dead

The gateway currently routes ~23k requests/month through 8 providers. Nine more providers have valid API keys but are either disabled, blocked by cooldowns, or simply have no pool entry. Three more providers have no keys at all. This analysis treats each as a distinct strategic opportunity.

---

## Providers by current state

### Never routed (have key, no pool entry)

| Provider | Key | Last 30d traffic | Failure rate | Models available (catalog) |
|---|---|---|---|---|
| **cohere** | `…fMNM` | 0 | — | ~20 (Rerank V3, Command R, Embed, English models) |
| **voyage** | none | 0 | — | ~10 (Rerank V2, Code embeddings, LLM rerank) |
| **jina** | none | 0 | — | ~10 (Rerank v2, Multimodal embeddings, multimodal LLM) |

**Rerank is the single biggest unused capability.** The gateway has a complete `/v1/rerank` route in `providers/rerank.py` supporting cohere/voyage/jina backends. Cohere has a live key (`last4=fMNM`). The endpoint works. It's simply not configured in any pool.

**Cohere rerank = immediate win.** The `RerankHandler` class at `providers/rerank.py:111` round-robins between cohere → voyage → jina. Cohere is the only one with a working key.

### Blocked by 100% failure rate (not a route problem — key/quota problem)

| Provider | Key | Last 30d traffic | Failure rate | Likely cause |
|---|---|---|---|---|
| **cerebras** | `…a11e` | 56 | 100% | API key invalid (manual-disable-2026-09-13) |
| **nvidia** | `…d1386` | 13 | 100% | API key invalid (manual-disable-2026-09-13) |
| **github-copilot** | `…1b649` | 16 | 100% | OAuth broken (manual-disable-2026-09-13) |

These three are `disabled_provider=1, passthrough_disabled=1` in the DB. The keys themselves may be revoked, expired, or under a different quota. **Action required:** rotate/refresh keys, then re-enable.

### Underperforming but working

| Provider | Last 30d traffic | Failure rate | Error profile |
|---|---|---|---|
| **groq** | 413 | 84.0% | API key invalid or quota-exhausted |
| **workers-ai** | 1,539 | 91.0% | Free tier exhausted daily (budget: ~5k tokens/day) |
| **google** | 378 | 69.8% | Mixed — many 404s (nonexistent models), key works for supported models |
| **opencode-go** | 896 | 20.6% | Some models failing (e.g. vision) |
| **local-llm** | 1,595 | 21.6% | Timeout-heavy (30s limit too aggressive for Jetson) |
| **mlx-mac** | 1,109 | 24.6% | Timeout-heavy (30s limit too aggressive for MLX) |

---

## Strategic opportunities

### 1. Enable Cohere rerank — no-cost immediate win

**What it enables:** Retrieval-augmented generation (RAG) with proper semantic reranking. Every long-context query (multi-document coding, file analysis, context window overfill) benefits from reranking.

**How:**
- `cohere` is already in the provider registry (`config.py:336`) with `rerank_path: "/v1/rerank"`
- `rerank.py:35` lists `_DEFAULT_PROVIDER_ORDER = ("cohere", "voyage", "jina")`
- Cohere has a live key in the DB
- The `/v1/rerank` endpoint is live and the handler routes to cohere first
- **No code changes needed.** Just verify the key works with a smoke test

**Verification:**
```bash
kubectl exec -n hermes tusker-gateway-dd5755dd5-4krz8 -- \
  python3 -c "from tusker_gateway.providers.rerank import RerankHandler; print(RerankHandler)"
```

### 2. Add Google Gemini models to `code` pool (multimodal)

**What it enables:** The `code` pool is text-only except for MiniMax-M3 (which has `input_modalities: ["text", "image"]`). Google Gemini has native multimodal support, making it the cheapest path to adding vision to code tasks.

**What works (quality ≥ 70):**
- `gemini-3-flash-preview` — score 71.1, 88.9% success
- `gemini-3.1-flash-lite-preview` — score 73.7, 90.9% success  
- `gemini-flash-lite-latest` — score 74.4, 80.0% success
- `gemini-2.5-flash-lite` — score 70.1, 81.8% success
- `gemini-robotics-er-2-preview` — score 70.0, 87.5% success

**Config change (k8s/deployment.yaml):**
Add these to `TUSKER_POOL_CODE` as `input_modalities: ["text", "image"]`.

**Risk:** Google routes have 69.8% overall failure rate, but the high-performing models above are the survivors. The bad ones (nonexistent models, video-preview, audio-preview) will naturally fail and trigger cooldown. Use only the proven models.

### 3. Add Workers AI as a low-priority overflow layer

**What it enables:** Free-tier image processing. Workers AI has 31 catalog models, many with `input_modalities: ["text", "image"]`, all at $0 cost (Cloudflare free tier). Currently routing llama models, which are generic. The real value is **free image encoding for the privacy pool** (no image models in privacy today).

**Best models (100% success, image-capable):**
- `@cf/meta/llama-4-scout-17b-16e-instruct` — 100% success
- `@cf/meta/llama-3.3-70b-instruct-fp8-fast` — 100% success

**Usage pattern:** Works only until daily quota (~100-200 requests), then 429. Already configured with `zdr=True` for privacy compliance. Good for short-burst image processing.

**Config change:** Already in `TUSKER_POOL_PRIVACY`. The problem is quota — can't be primary. Add a longer cooldown override (`TUSKER_WORKERS_AI_COOLDOWN_SECS: 3600`) to stop retrying after daily quota exhaustion instead of flooding with 429s.

### 4. Add Alibaba (DeepSeek) to code pool — cost-optimize long-context

**What it enables:** `alibaba/deepseek-v4-flash-0731` is the cheapest long-context model available (token-cost at 30% of other providers). Already in the privacy pool. Worth adding to code as a cost layer.

**Quality:** 100% success (203 calls, 45 failures = 22.2% overall, but the flash model itself is reliable).

**Config change:** Add `{"provider": "alibaba", "model": "deepseek-v4-flash-0731", "input_modalities": ["text"]}` to `TUSKER_POOL_CODE`.

### 5. Add Ollama Cloud models as a mid-tier fallback

**What it enables:** Ollama Cloud has 20+ catalog models at 80.0+ quality scores, mostly Chinese models (GLM, Kimi) that work well for structured output tasks. The gateway already routes through them. The issue is that the cloud models are in the `code` and `privacy` pools, but the pool assignment is inconsistent.

**Best models by quality:**
- `glm-5.3-flash` — score 80.0, 584 calls, 100% success ← most reliable
- `glm-5.3` — score 80.0, 118 calls, 100% success
- `glm-5.3-flash` — already in code pool
- `kimi-k2.7-code` — score 79.8, 280 calls, 99.6% success ← best for coding
- `minimax-m3` — score 79.1, 88 calls, 98.9% success
- `gpt-oss:20b` — score 100.0, 1280 calls, 99.0% success

**Config change:** Move `ollama-cloud` from code pool to dedicated low-priority fallback. Use only when all paid providers are exhausted. The 11.6% error rate is mostly quota-exhaustion.

### 6. Increase local-llm timeout — fix privacy pool 502s

**What it enables:** The privacy pool currently 502s when workers-ai quota is exhausted AND local-llm times out at 30s. Both Jetson (10.0.0.212) and MLX Mac (10.0.0.141) are responsive but slow for inference (first token at ~15-20s for a 7B model). A 60s timeout would allow 2-3x more token generation before timeout.

**Config change:**
```yaml
# In deployment.yaml, add to env:
- name: TUSKER_LOCAL_LLM_TIMEOUT_SECS
  value: "60"
```

### 7. Fix NVIDIA key — enable free GPU models

**What it enables:** NVIDIA offers free GPU models (Llama, DeepSeek-R1, Nemotron, Gemma, Qwen3) for evaluation. All failed with 100% error rate, but the key is in the DB. The failure is likely:
1. API key revoked (check NVIDIA NGC portal)
2. Wrong base URL (NVIDIA NIM uses different endpoints for different model families)

**To fix:**
- Verify key at `https://build.nvidia.com/nim/` portal
- Update key via: `python -m tusker_gateway.tools.rotate_provider_key nvidia NEW_KEY`
- Then re-enable: `UPDATE tusker_config_provider_settings SET enabled=1, disabled_provider=0 WHERE provider='nvidia'`

### 8. Fix Cerebras key — enable free fast inference

**What it enables:** Cerebras has the fastest inference (100+ tok/s) for lightweight models. 100% failure rate suggests the key is invalid.

**To fix:** Same as NVIDIA — verify key at `https://cloud.cerebras.ai`, rotate, re-enable.

---

## Implementation priority

| Priority | Change | Risk | Reward |
|---|---|---|---|
| P0 | Enable Cohere rerank | Zero (key exists) | Semantic search for RAG |
| P1 | Increase local-llm timeout to 60s | Low (env var only) | Fewer 502s in privacy pool |
| P1 | Add Gemini flash models to code pool | Low (env var only) | Multimodal for code tasks |
| P2 | Add alibaba/deepseek-v4-flash to code pool | Low (env var only) | Long-context cost savings |
| P2 | Fix NVIDIA/Cerebras keys + re-enable | Medium (key rotation) | Free fast inference |
| P3 | Fix opencode-go vision models | Low (model list audit) | Multimodal overflow |
| P3 | Add ollama-cloud kimi-k2.7-code to code pool | Low | Coding-optimized fallback |

---

## Appendix: what to NOT add

| Provider | Reason |
|---|---|
| **arcee** | No key, no rerank path, no useful chat models for gateway scale |
| **arliai** | No key, low-tier provider, 24% failure rate on existing alibaba |
| **openai** | No key, direct OpenAI API not needed (openai-codex covers it) |
| **voyage** | No key (cohere covers rerank, voyage is redundant) |
| **jina** | No key (cohere covers rerank, jina is redundant) |
| **synthetic** | 18.6% failure rate, quota-exhausted, useful only for synthetic benchmarks |
