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

# Explicit provider prefix marker (e.g. "github-copilot::gpt-5.5").
PROVIDER_PREFIX = "::"

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

    # Provider prefix passthrough
    provider, bare = split_model(model)
    if provider:
        return Route(kind="passthrough", provider=provider, model=bare)

    # Slash-form provider/model
    if "/" in model:
        # Swarm role markers are not passthrough
        for marker in SWARM_ROLE_MARKERS:
            if model.startswith(marker):
                return Route(kind="swarm", model=model)
        provider, _, bare = model.partition("/")
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
