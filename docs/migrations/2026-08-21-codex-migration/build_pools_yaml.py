#!/usr/bin/env python3
"""Build the FINAL TUSKER_POOL_CODE / TUSKER_POOL_PRIVACY JSON values for deployment.yaml.

Mirrors build_pools.py logic but outputs ready-to-paste yaml env values.
"""
import json, sys

GATEWAY_PROVIDERS = {
    "openai", "openrouter", "groq", "zai", "google", "cerebras", "cohere",
    "minimax", "synthetic", "ollama-cloud", "opencode-go", "opencode-zen",
    "openai-codex", "github-copilot", "github-copilot-enterprise", "local-llm",
}

CODEX_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]


def parse(path):
    code, priv = [], []
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("CODE\t"):
                code.append(line.split("\t", 1)[1])
            elif line.startswith("PRIVACY\t"):
                priv.append(line.split("\t", 1)[1])
    return code, priv


def filter_(models):
    out, seen = [], set()
    for m in models:
        provider, _, model = m.partition("/")
        if provider in {"openai", "mlx-mac", "ollama-mac", "ollama", "xiaomi"}:
            continue
        if provider not in GATEWAY_PROVIDERS:
            continue
        key = (provider, model)
        if key in seen:
            continue
        seen.add(key)
        out.append({"provider": provider, "model": model})
    return out


def add_codex(pool):
    seen = {(m["provider"], m["model"]) for m in pool}
    for m in CODEX_MODELS:
        if ("openai-codex", m) not in seen:
            pool.append({"provider": "openai-codex", "model": m})
            seen.add(("openai-codex", m))
    return pool


def to_pool_json(models, ctx=128000, zdr=False):
    return json.dumps({"models": models, "context_window": ctx, "zdr": zdr}, separators=(",", ":"))


if __name__ == "__main__":
    src = sys.argv[1]
    code_raw, priv_raw = parse(src)
    code = filter_(code_raw)
    priv = filter_(priv_raw)
    code = add_codex(code)
    priv = add_codex(priv)
    print("=== TUSKER_POOL_CODE ===")
    print(to_pool_json(code))
    print()
    print("=== TUSKER_POOL_PRIVACY ===")
    print(to_pool_json(priv, zdr=True))
    print()
    print(f"counts: code={len(code)}, privacy={len(priv)}")
