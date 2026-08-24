"""Configuration loaded from environment variables."""
from __future__ import annotations

import json
import os
import secrets
from typing import Any
from dataclasses import dataclass
from typing import Literal

import logging

logger = logging.getLogger(__name__)


@dataclass
class ProviderConfig:
    """Normalized provider configuration."""
    name: str
    kind: Literal["bearer", "oauth", "local", "upstream"]
    base_url: str
    chat_path: str
    auth_env: str | None = None
    pool_env: str | None = None
    model_header: str | None = None
    auth_type: str = "bearer"
    zdr_ok: bool = False
    heavyweight: bool = False


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
    config["api_keys"] = [k.strip() for k in _parse_env_list("API_KEYS") if k.strip()]
    if not config["api_keys"]:
        config["api_keys"] = [secrets.token_hex(32)]

    # Virtual model name advertised to clients
    config["model_name"] = os.environ.get("MODEL_NAME", "tusker-gateway")
    config["pools"] = _load_pools()
    config["excluded_providers"] = [p.strip() for p in _parse_env_list("EXCLUDED_PROVIDERS") if p.strip()]

    # Normalized provider registry and API-key map.
    config["providers"] = _load_providers()
    raw_providers: dict[str, str] = {}
    json_raw = os.environ.get("PROVIDER_API_KEYS", "").strip()
    if json_raw:
        try:
            parsed = json.loads(json_raw)
            if isinstance(parsed, dict):
                raw_providers.update({str(k).lower(): str(v) for k, v in parsed.items() if v})
        except (TypeError, ValueError):
            pass
    for key, value in os.environ.items():
        if key.startswith("PROVIDER_") and key.endswith("_API_KEY") and value:
            provider = key[len("PROVIDER_") : -len("_API_KEY")].lower()
            raw_providers[provider] = value
    config["provider_api_keys"] = raw_providers

    # Map Hermes-style single-provider env keys into the normalized API-key map.
    # Each entry: target provider name -> source env var name (or already-present key).
    _ENV_KEY_ALIASES = {
        "openrouter": ["OPENROUTER_API_KEY"],
        "groq": ["GROQ_API_KEY"],
        "zai": ["ZAI_API_KEY", "GLM_API_KEY"],
        "xiaomi": ["XIAOMI_MIMO_API_KEY", "MIMO_API_KEY", "XIAOMI_API_KEY"],
        "arliai": ["ARLIAI_API_KEY"],
        "google": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "cerebras": ["CEREBRAS_API_KEY"],
        "cohere": ["COHERE_API_KEY"],
        "minimax": ["MINIMAX_API_KEY"],
        "synthetic": ["SYNTHETIC_API_KEY"],
        "ollama-cloud": ["OLLAMA_API_KEY", "OLLAMA_MAC_API_KEY"],
        "opencode-go": ["OPENCODE_GO_API_KEY"],
        "opencode-zen": ["OPENCODE_ZEN_API_KEY"],
        "github-copilot": ["GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"],
        "github-copilot-enterprise": ["GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"],

    }
    for provider, alias_keys in _ENV_KEY_ALIASES.items():
        if provider in raw_providers:
            continue
        for env_key in alias_keys:
            value = os.environ.get(env_key, "").strip()
            if value:
                raw_providers[provider] = value
                break
    config["provider_api_keys"] = raw_providers
    for key, value in os.environ.items():
        if key.startswith("PROVIDER_") and key.endswith("_API_KEY") and value:
            provider = key[len("PROVIDER_") : -len("_API_KEY")].lower()
            raw_providers[provider] = value
    config["provider_api_keys"] = raw_providers

    auth_file = os.environ.get("TUSKER_AUTH_FILE", "").strip()
    if not auth_file:
        from pathlib import Path as _Path
        auth_file = str(_Path.home() / ".hermes" / "auth.json")
    config["auth_file"] = auth_file
    config["codex_credentials"] = _parse_env_json_list("CODEX_CREDENTIALS")
    if not config["codex_credentials"]:
        try:
            from tusker_gateway.copilot_enroll import load_auth_file as _load_auth
            creds = _load_auth(auth_file)
            # Filter to openai-codex credentials for the codex pool
            config["codex_credentials"] = [
                c for c in creds
                if c.get("provider", "openai-codex") == "openai-codex"
            ]
            # If no provider tag is present, keep all creds (legacy list-format file).
            if not config["codex_credentials"] and any(
                "provider" not in c for c in creds
            ):
                config["codex_credentials"] = creds
        except (OSError, TypeError, ValueError):
            pass

    from pathlib import Path as _Path
    default_db = "/home/tusker/.hermes/model_quality.db" if _Path("/home/tusker").exists() else "/tmp/tusker-quality.db"
    config["quality_db_path"] = os.environ.get("QUALITY_DB_PATH", default_db)
    logger.info('config loaded: %d providers, %d pools, quality_db=%s', len(config.get("providers", {})), len(config.get("pools", {})), config["quality_db_path"])
    return config


def _load_providers() -> dict[str, ProviderConfig]:
    """Return the provider registry (defaults + optional JSON override from env)."""
    return _provider_registry_from_env()


def _provider_registry_from_env() -> dict[str, ProviderConfig]:
    registry = dict(DEFAULT_PROVIDER_REGISTRY)
    raw = os.environ.get("PROVIDER_REGISTRY_JSON", "").strip()
    if not raw:
        return registry
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return registry
    if not isinstance(data, dict):
        return registry
    for name, value in data.items():
        if not isinstance(value, dict):
            continue
        merged = {
            "name": str(name).lower(),
            "kind": value.get("kind", value.get("auth_type", "bearer")),
            "base_url": value["base_url"],
            "chat_path": value.get("chat_path", "/v1/chat/completions"),
            "auth_env": value.get("auth_env"),
            "pool_env": value.get("pool_env"),
            "model_header": value.get("model_header"),
            "auth_type": value.get("auth_type", value.get("kind", "bearer")),
            "zdr_ok": bool(value.get("zdr_ok", False)),
            "heavyweight": bool(value.get("heavyweight", False)),
        }
        registry[str(name).lower()] = ProviderConfig(**merged)
    return registry


DEFAULT_PROVIDER_REGISTRY: dict[str, ProviderConfig] = {
    "openai": ProviderConfig("openai", "bearer", "https://api.openai.com", "/v1/chat/completions", auth_env="OPENAI_API_KEY"),
    "openrouter": ProviderConfig("openrouter", "bearer", "https://openrouter.ai/api/v1", "/chat/completions", auth_env="OPENROUTER_API_KEY"),
    "groq": ProviderConfig("groq", "bearer", "https://api.groq.com/openai", "/v1/chat/completions", auth_env="GROQ_API_KEY"),
    "zai": ProviderConfig("zai", "bearer", "https://api.z.ai/api/coding/paas", "/v4/chat/completions", auth_env="GLM_API_KEY"),
    "xiaomi": ProviderConfig("xiaomi", "bearer", "https://token-plan-sgp.xiaomimimo.com", "/v1/chat/completions", auth_env="XIAOMI_MIMO_API_KEY"),
    "arliai": ProviderConfig("arliai", "bearer", "https://api.arliai.com", "/v1/chat/completions", auth_env="ARLIAI_API_KEY"),
    "google": ProviderConfig("google", "bearer", "https://generativelanguage.googleapis.com", "/v1beta/openai/chat/completions", auth_env="GEMINI_API_KEY"),
    "cerebras": ProviderConfig("cerebras", "bearer", "https://api.cerebras.ai", "/v1/chat/completions", auth_env="CEREBRAS_API_KEY"),
    "cohere": ProviderConfig("cohere", "bearer", "https://api.cohere.com/compatibility", "/v1/chat/completions", auth_env="COHERE_API_KEY"),
    "minimax": ProviderConfig("minimax", "bearer", "https://api.minimax.io", "/v1/chat/completions", auth_env="MINIMAX_API_KEY"),
    "synthetic": ProviderConfig("synthetic", "bearer", "https://api.synthetic.new", "/v1/chat/completions", auth_env="SYNTHETIC_API_KEY"),
    "ollama-cloud": ProviderConfig("ollama-cloud", "bearer", "https://ollama.com", "/v1/chat/completions", auth_env="OLLAMA_API_KEY"),
    "opencode-go": ProviderConfig("opencode-go", "bearer", "https://opencode.ai/zen/go/v1", "/chat/completions", auth_env="OPENCODE_GO_API_KEY", zdr_ok=True),
    "opencode-zen": ProviderConfig("opencode-zen", "bearer", "https://opencode.ai/zen", "/v1/chat/completions", auth_env="OPENCODE_ZEN_API_KEY"),
    "openai-codex": ProviderConfig("openai-codex", "codex", "https://chatgpt.com/backend-api/codex", "/responses", pool_env="CODEX_CREDENTIALS", auth_type="codex", model_header="x-openai-gpt-model", zdr_ok=True),
    "github-copilot": ProviderConfig("github-copilot", "oauth", "https://api.githubcopilot.com", "/chat/completions", pool_env="CODEX_CREDENTIALS", auth_type="oauth", model_header="x-github-gpt-model"),
    "github-copilot-enterprise": ProviderConfig("github-copilot-enterprise", "oauth", "https://api.githubcopilot.com", "/chat/completions", pool_env="CODEX_CREDENTIALS", auth_type="oauth", model_header="x-github-gpt-model"),
    "local-llm": ProviderConfig("local-llm", "local", "http://localhost:11434", "/v1/chat/completions"),
}


def _provider_registry_from_env() -> dict[str, ProviderConfig]:
    registry = dict(DEFAULT_PROVIDER_REGISTRY)
    raw = os.environ.get("PROVIDER_REGISTRY_JSON", "").strip()
    if not raw:
        return registry
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return registry
    if not isinstance(data, dict):
        return registry
    for name, value in data.items():
        if not isinstance(value, dict):
            continue
        merged = {
            "name": str(name).lower(),
            "kind": value.get("kind", value.get("auth_type", "bearer")),
            "base_url": value["base_url"],
            "chat_path": value.get("chat_path", "/v1/chat/completions"),
            "auth_env": value.get("auth_env"),
            "pool_env": value.get("pool_env"),
            "model_header": value.get("model_header"),
            "auth_type": value.get("auth_type", value.get("kind", "bearer")),
            "zdr_ok": bool(value.get("zdr_ok", False)),
            "heavyweight": bool(value.get("heavyweight", False)),
        }
        registry[str(name).lower()] = ProviderConfig(**merged)
    return registry

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
                # Mix of free/cheap models (kept) and heavyweight slugs (dropped
                # automatically by the heavyweight gate in pools.py).
                {"provider": "github-copilot", "model": "gpt-5.5"},
                {"provider": "github-copilot", "model": "claude-sonnet-4.6"},
                {"provider": "openai-codex", "model": "gpt-5.6-luna"},
                {"provider": "openai-codex", "model": "gpt-5.4-mini"},
                {"provider": "openrouter", "model": "openai/gpt-oss-20b:free"},
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
