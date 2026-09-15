"""Shared helpers for resolving per-provider base URLs.

Both :class:`~tusker_gateway.providers.embed.EmbedHandler` and
:class:`~tusker_gateway.providers.rerank.RerankHandler` need the same logic
to figure out which base URL to talk to for a given provider: an env
override wins, otherwise the registry entry's ``base_url``, otherwise a
built-in local fallback (e.g. ``host.docker.internal`` for ``local-llm``).

Centralising this here prevents the two handlers drifting out of sync as
new local backends (mlx-mac, local-rerank, etc.) get added.
"""
from __future__ import annotations

import os
from typing import Any


def _provider_value(provider_config: Any, field: str, default: Any = None) -> Any:
    """Read ``field`` from either a dict-style or dataclass provider config.

    Keeps parity with the other helpers in ``providers/embed.py``.
    """
    if isinstance(provider_config, dict):
        return provider_config.get(field, default)
    return getattr(provider_config, field, default)


def is_local_provider(provider_config: Any) -> bool:
    """True when the provider is unauthenticated (kind/auth_type == 'local')."""
    return (
        _provider_value(provider_config, "kind", "") == "local"
        or _provider_value(provider_config, "auth_type", "") == "local"
    )


# Built-in fallbacks applied when neither env nor registry supplies a
# base_url. These keep working out-of-the-box for local Ollama in a
# Docker-desktop-style setup. Add entries here when new local providers
# appear in default provider orders.
_LOCAL_BASE_URL_DEFAULTS: dict[str, str] = {
    "local-llm": "http://host.docker.internal:11434",
    "mlx-mac": "http://host.docker.internal:11435",
}


def resolve_base_url(
    provider: str, provider_config: Any, *, env_prefix: str = "TUSKER_EMBED"
) -> str:
    """Resolve the base URL for a provider in priority order:

    1. ``<env_prefix>_<PROVIDER>_BASE_URL`` env override (legacy
       ``HERMES_<OP>_<PROVIDER>_BASE_URL`` also accepted).
    2. ``base_url`` from the provider registry entry.
    3. Built-in default for known local backends
       (e.g. ``http://host.docker.internal:11434`` for ``local-llm``).

    Returns an empty string when nothing matched; callers should treat
    that as "no usable URL".
    """
    suffix = provider.upper().replace("-", "_")
    legacy = env_prefix.replace("TUSKER_", "HERMES_")
    env_value = (
        os.environ.get(f"{env_prefix}_{suffix}_BASE_URL", "").strip()
        or os.environ.get(f"{legacy}_{suffix}_BASE_URL", "").strip()
    )
    if env_value:
        return env_value
    configured = str(_provider_value(provider_config, "base_url", "") or "").strip()
    if configured:
        return configured
    return _LOCAL_BASE_URL_DEFAULTS.get(provider, "")
