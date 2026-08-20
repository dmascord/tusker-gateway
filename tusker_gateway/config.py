"""Configuration loaded from environment variables."""
from __future__ import annotations

import json
import os
import secrets
from typing import Any


class PoolConfig:
    """Configuration for a single model pool."""

    def __init__(
        self,
        name: str,
        models: list[dict[str, Any]],
        *,
        context_window: int = 128_000,
        zdr: bool = False,
        provider_warmup_secs: int = 300,
    ):
        self.name = name
        self.models = models
        self.context_window = context_window
        self.zdr = zdr
        self.provider_warmup_secs = provider_warmup_secs

    def __repr__(self) -> str:
        return f"PoolConfig(name={self.name!r}, models={len(self.models)})"


def _parse_env_list(env_var: str) -> list[str]:
    """Parse a comma-separated env var into a list of strings."""
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _parse_env_json_list(env_var: str) -> list[dict[str, Any]]:
    """Parse an env var as a JSON array of objects."""
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        return [parsed]
    except json.JSONDecodeError:
        return []


def load_config() -> dict[str, Any]:
    """Load gateway configuration from environment variables."""
    config: dict[str, Any] = {}

    config["host"] = os.environ.get("HOST", "0.0.0.0")
    config["port"] = int(os.environ.get("PORT", "8642"))

    # API key (fail-closed)
    config["api_keys"] = [
        k.strip() for k in _parse_env_list("API_KEYS") if k.strip()
    ]
    if not config["api_keys"]:
        config["api_keys"] = [secrets.token_hex(32)]
        # In production, fail closed:
        # raise RuntimeError("API_KEYS env var must be set")

    # Virtual model name advertised to clients
    config["model_name"] = os.environ.get("MODEL_NAME", "tusker-gateway")

    # Pool definitions from JSON env vars
    config["pools"] = _load_pools()

    # Excluded providers (for privacy pools — excludes heavyweight models)
    config["excluded_providers"] = [
        p.strip() for p in _parse_env_list("EXCLUDED_PROVIDERS") if p.strip()
    ]
    # Codex OAuth credentials (JSON list of {token, refresh_token, ...})
    # Provider API keys: map provider name → key
    # Accepts either JSON dict: {"openrouter": "sk-..."}
    # Or individual env vars: PROVIDER_OPENROUTER_API_KEY=sk-...
    raw_providers: dict[str, str] = {}
    json_raw = os.environ.get("PROVIDER_API_KEYS", "").strip()
    if json_raw:
        try:
            parsed = json.loads(json_raw)
            if isinstance(parsed, dict):
                raw_providers = {k.lower(): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            pass
    # Individual env vars override JSON
    for key, value in os.environ.items():
        if key.startswith("PROVIDER_") and key.endswith("_API_KEY"):
            provider = key[len("PROVIDER_") : -len("_API_KEY")].lower()
            raw_providers[provider] = value
    # Codex OAuth credentials (JSON list of {token, refresh_token, ...})
    config["codex_credentials"] = _parse_env_json_list("CODEX_CREDENTIALS")
    config["provider_api_keys"] = raw_providers
    # Quality DB path — use /tmp for local dev when /home/tusker doesn't exist
    from pathlib import Path as _Path
    default_db = "/home/tusker/.hermes/model_quality.db"
    if not _Path("/home/tusker").exists():
        default_db = "/tmp/tusker-quality.db"
    config["quality_db_path"] = os.environ.get("QUALITY_DB_PATH", default_db)
    return config

def _load_pools() -> dict[str, PoolConfig]:
    """Load pool definitions from environment variables.

    Each TUSKER_POOL_<NAME> env var contains a JSON object:
    {
        "models": [
            {"provider": "github-copilot", "model": "gpt-5.5"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol", "heavyweight": true}
        ],
        "context_window": 128000,
        "zdr": false
    }
    """
    pools: dict[str, PoolConfig] = {}

    for key, value in os.environ.items():
        if not key.startswith("TUSKER_POOL_"):
            continue
        pool_name = key[len("TUSKER_POOL_") :].lower().replace("_", "-")
        if not pool_name:
            continue
        try:
            data = json.loads(value)
            models = data.get("models", [])
            if not models:
                continue
            pools[pool_name] = PoolConfig(
                name=pool_name,
                models=models,
                context_window=data.get("context_window", 128_000),
                zdr=data.get("zdr", False),
            )
        except (json.JSONDecodeError, TypeError):
            continue

    # Defaults for standard pools if not explicitly configured
    if "code" not in pools:
        pools["code"] = PoolConfig(
            name="code",
            models=[
                {"provider": "github-copilot", "model": "gpt-5.5"},
                {"provider": "github-copilot", "model": "claude-sonnet-4.6"},
            ],
        )
    if "privacy" not in pools:
        pools["privacy"] = PoolConfig(
            name="privacy",
            models=[
                {"provider": "openai-codex", "model": "gpt-5.6-luna"},
                {"provider": "openai-codex", "model": "gpt-5.4-mini"},
            ],
            zdr=True,
        )
    if "premium" not in pools:
        pools["premium"] = PoolConfig(
            name="premium",
            models=[
                {"provider": "openai-codex", "model": "gpt-5.6-sol"},
                {"provider": "openai-codex", "model": "gpt-5.6-terra"},
            ],
        )
    if "swarm" not in pools:
        pools["swarm"] = PoolConfig(
            name="swarm",
            models=[
                {"provider": "github-copilot", "model": "gpt-5.5"},
                {"provider": "github-copilot", "model": "claude-sonnet-4.6"},
            ],
        )

    return pools
