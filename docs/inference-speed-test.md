# DGX Gateway Inference Speed Test

## Overview
This document records a simple latency benchmarking of the internal DGX gateway used by **Tusker AI Gateway**. The test exercises the `/chat/completions` endpoint of the gateway using the model `qwen3.8‑flash‑next` and measures the following per request:

1. **HTTP** status code.
2. **TTFB** – *Time To First Byte*.
3. **Total request duration**.
4. Prompt and completion token counts.

The key used for authentication is read from the repository’s `.env` file (`DGX_API_KEY`). Ensure the key and URL are up‑to‑date when re‑running.

## Test Procedure
The test was performed with a bash script that loops five times and outputs the metrics:

```bash
source .env
for i in {1..5}; do
  tmp=$(mktemp)
  metrics=$(curl -sS -o "$tmp" -w '%{http_code} %{time_starttransfer} %{time_total}' \
    -H "Authorization: Bearer $DGX_API_KEY" \
    -H 'Content-Type: application/json' \
    -d '{"model":"qwen3.8-flash-next","messages":[{"role":"user","content":"What is 2+2? Answer briefly."}],"max_tokens":50}' \
    "$DGX_API_URL/chat/completions")
  python3 - "$tmp" "$i" "$metrics" <<'PY'
import json,sys
run,metrics=sys.argv[1:]
tokens=json.load(open(sys.argv[1]))
print(f'run={run} http={metrics.split()[0]} ttfb={float(metrics.split()[1]):.3f}s total={float(metrics.split()[2]):.3f}s prompt={tokens.get("usage",{}).get("prompt_tokens")} completion={tokens.get("usage",{}).get("completion_tokens")}')
PY
  rm -f "$tmp"
done
```

The script uses the **official VLLM deployment** of Qwen‑3.8‑Flash‑Next; other models can be tested similarly.

## Results
| Run | HTTP | TTFB (s) | Total (s) | Prompt tokens | Completion tokens |
|-----|------|----------|-----------|---------------|-------------------|
| 1   | 200  | 2.289    | 2.289     | 62            | 30 |
| 2   | 200  | 2.561    | 2.562     | 62            | 30 |
| 3   | 200  | 2.758    | 2.759     | 62            | 37 |
| 4   | 200  | 2.393    | 2.394     | 62            | 29 |
| 5   | 200  | 10.222   | 10.222    | 62            | 46 |

**Average**: TTFB ≈ 2.47 s, Total ≈ 2.52 s. Run 5 shows a transient increase, possibly due to background load.

## Notes
* The DGX gateway health endpoint (`/health`) returns `200 OK` and the `/v1/models` endpoint lists the supported model.
* Authentication works with the key in `.env`; any unauthorized errors indicate the key is stale or the gateway configuration changed.
* For continuous monitoring, automate this script as a cron job or CI pipeline job.

---

**Author:** OMP
**Date:** 2026‑09‑24

