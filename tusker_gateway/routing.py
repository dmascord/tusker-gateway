"""Role-alias routing and passthrough detection.

Maps the virtual role aliases (hermes-code, hermes-privacy, hermes-premium,
hermes-swarm) and provider-prefixed model ids to concrete dispatch decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Route:
    """A resolved dispatch decision."""

    kind: str  # "pool" | "passthrough" | "swarm"
    pool_name: str | None = None
    provider: str | None = None
    model: str | None = None
    role: str | None = None


# Virtual role aliases that map to pools.
POOL_ALIASES = {
    "hermes-code": "code",
    "hermes-privacy": "privacy",
    "hermes-premium": "premium",
    "hermes-swarm": "swarm",
}

# Model ids advertised by the legacy Hermes endpoint. Provider-prefixed ids
# continue through the normal passthrough path; the legacy control-plane and
# local-only ids below need a best-effort gateway pool equivalent after the
# hostname migration because their original backends are not present here.
LEGACY_MODEL_IDS = (
    "github-copilot-enterprise/claude-haiku-4.5",
    "github-copilot-enterprise/claude-opus-4.6",
    "github-copilot-enterprise/claude-sonnet-4.5",
    "github-copilot-enterprise/claude-sonnet-4.6",
    "github-copilot-enterprise/gemini-3.1-pro-preview",
    "github-copilot-enterprise/gpt-4.1",
    "github-copilot-enterprise/gpt-4o-mini",
    "github-copilot-enterprise/gpt-5-mini",
    "github-copilot-enterprise/gpt-5.3-codex",
    "github-copilot-enterprise/gpt-5.4",
    "github-copilot-enterprise/gpt-5.4-mini",
    "github-copilot-enterprise/gpt-5.5",
    "github-copilot-enterprise/gpt-5.6-luna",
    "github-copilot-enterprise/gpt-5.6-sol",
    "github-copilot-enterprise/gpt-5.6-terra",
    "hermes-agent",
    "hermes-agentic-full",
    "hermes-agentic-remote",
    "hermes-gateway/hermes-balanced",
    "hermes-gateway/hermes-duplicate-pr",
    "hermes-gateway/hermes-fast",
    "hermes-gateway/hermes-reflect",
    "hermes-gateway/hermes-translator",
    "hermes-gateway/hermes-triage",
    "hermes-gateway/roo-architect",
    "hermes-gateway/roo-ask",
    "hermes-gateway/roo-debug",
    "hermes-reranker",
    "mlx-mac/qwen3-coder-30b-a3b-instruct-4bit",
)

LEGACY_POOL_COMPAT_ALIASES = {
    "hermes-agent": "code",
    "hermes-agentic-full": "code",
    "hermes-agentic-remote": "code",
    "hermes-gateway/hermes-balanced": "code",
    "hermes-gateway/hermes-duplicate-pr": "code",
    "hermes-gateway/hermes-fast": "code",
    "hermes-gateway/hermes-reflect": "code",
    "hermes-gateway/hermes-translator": "code",
    "hermes-gateway/hermes-triage": "code",
    "hermes-gateway/roo-architect": "code",
    "hermes-gateway/roo-ask": "code",
    "hermes-gateway/roo-debug": "code",
    "hermes-reranker": "code",
    "mlx-mac/qwen3-coder-30b-a3b-instruct-4bit": "code",
}

# Explicit provider prefix marker (e.g. "github-copilot::gpt-5.5").
PROVIDER_PREFIX = "::"
GATEWAY_PROVIDER = "tusker-gateway"

# Models sent directly to their provider (passthrough) with no pool
# selection. Identified by a slash (provider/model) or an explicit
# provider prefix.
SWARM_ROLE_MARKERS = ("hermes-gateway/", "hermes-reflect/")


def split_model(model: str | None) -> tuple[str | None, str | None]:
    """Split a 'provider::model' string into (provider, model).

    Returns (None, None) when there is no provider prefix.
    """
    if not model or PROVIDER_PREFIX not in model:
        return None, model
    provider, _, rest = model.partition(PROVIDER_PREFIX)
    return provider or None, rest or None


def resolve_route(model: str | None, body: dict[str, Any]) -> Route:
    """Resolve the route for a requested model id.

    Priority:
    1. Virtual pool alias (hermes-code, etc.) → pool route
    2. Explicit provider prefix (provider::model) → passthrough
    3. Slash-form (provider/model) that is NOT a swarm role → passthrough
    4. Swarm role markers → swarm route
    5. Unknown / virtual alias (equals configured model_name) → default pool
    """
    if not model:
        # Default to the code pool.
        return Route(kind="pool", pool_name="code", role="hermes-code")

    # Pool aliases
    if model in POOL_ALIASES:
        return Route(
            kind="pool",
            pool_name=POOL_ALIASES[model],
            role=model,
        )

    if model in LEGACY_POOL_COMPAT_ALIASES:
        return Route(
            kind="pool",
            pool_name=LEGACY_POOL_COMPAT_ALIASES[model],
            role=model,
        )

    # Provider prefix passthrough
    provider, bare = split_model(model)
    if provider:
        if provider.lower() == GATEWAY_PROVIDER and bare in POOL_ALIASES:
            return Route(
                kind="pool",
                pool_name=POOL_ALIASES[bare],
                role=bare,
            )
        return Route(kind="passthrough", provider=provider, model=bare)

    # Slash-form provider/model
    if "/" in model:
        provider, _, bare = model.partition("/")
        if provider.lower() == GATEWAY_PROVIDER and bare in POOL_ALIASES:
            return Route(
                kind="pool",
                pool_name=POOL_ALIASES[bare],
                role=bare,
            )
        # Swarm role markers are not passthrough
        for marker in SWARM_ROLE_MARKERS:
            if model.startswith(marker):
                return Route(kind="swarm", model=model)
        return Route(kind="passthrough", provider=provider, model=bare)

    # Unknown bare model (no pool alias, no slash) → treat as provider/model
    # only if a configured provider exists, else default code pool.
    return Route(kind="code", pool_name="code", role="hermes-code")


def is_virtual_alias(model: str | None, configured_model: str) -> bool:
    """Return True if `model` is the advertised virtual alias itself.

    A client echoing /v1/models' advertised id back means "use the gateway
    default", not a real dispatchable model. Guard against persisting it.
    """
    if not model:
        return False
    return model == configured_model
