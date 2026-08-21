#!/usr/bin/env python3
"""Build the FINAL TUSKER_POOL_CODE / TUSKER_POOL_PRIVACY JSON values.

Sources (in order):
- Hermes live pools (code, privacy) — captured via kubectl exec
- Codex catalog (8 models) — confirmed accessible via all 3 OAuth accounts

Filters:
- Drop providers that have no API key in the gateway (openai, mlx-mac, xiaomi, ollama-mac)
- Drop providers not in gateway's DEFAULT_PROVIDER_REGISTRY
- Drop 'ollama' (local Mac) — needs base-URL remap; ollama-cloud is the cloud equivalent

Adds:
- openai-codex models from the codex catalog (gated by valid OAuth credentials we have)
"""
import json, sys

GATEWAY_PROVIDERS = {
    "openai", "openrouter", "groq", "zai", "google", "cerebras", "cohere",
    "minimax", "synthetic", "ollama-cloud", "opencode-go", "opencode-zen",
    "openai-codex", "github-copilot", "github-copilot-enterprise", "local-llm",
}

# Codex catalog (public, confirmed accessible by all 3 OAuth accounts)
CODEX_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]


def parse_hermes_list(path):
    code, priv = [], []
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("CODE\t"):
                code.append(line.split("\t", 1)[1])
            elif line.startswith("PRIVACY\t"):
                priv.append(line.split("\t", 1)[1])
    return code, priv


def filter_models(models, allow_openai_codex=False):
    out, seen = [], set()
    for m in models:
        provider, _, model = m.partition("/")
        if provider in {"openai", "mlx-mac", "ollama-mac", "ollama", "xiaomi"}:
            continue
        if provider not in GATEWAY_PROVIDERS:
            continue
        if provider == "openai-codex" and not allow_openai_codex:
            continue
        key = (provider, model)
        if key in seen:
            continue
        seen.add(key)
        out.append({"provider": provider, "model": model})
    return out


def add_codex(pool):
    """Append openai-codex models (dedup) if not already present."""
    seen = {(m["provider"], m["model"]) for m in pool}
    for m in CODEX_MODELS:
        key = ("openai-codex", m)
        if key not in seen:
            pool.append({"provider": "openai-codex", "model": m})
            seen.add(key)
    return pool


def to_pool_json(models, ctx=128000, zdr=False):
    return json.dumps({"models": models, "context_window": ctx, "zdr": zdr}, separators=(",", ":"))


if __name__ == "__main__":
    src = sys.argv[1]
    code_raw, priv_raw = parse_hermes_list(src)
    code = filter_models(code_raw, allow_openai_codex=True)
    priv = filter_models(priv_raw, allow_openai_codex=True)
    code = add_codex(code)
    priv = add_codex(priv)
    print("=== TUSKER_POOL_CODE ===")
    print(to_pool_json(code))
    print()
    print("=== TUSKER_POOL_PRIVACY ===")
    print(to_pool_json(priv, zdr=True))
    print()
    print(f"counts: code={len(code)}, privacy={len(priv)}")
    print("code providers:", sorted({m["provider"] for m in code}))
    print("privacy providers:", sorted({m["provider"] for m in priv}))
