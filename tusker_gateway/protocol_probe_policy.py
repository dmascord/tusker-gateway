"""Safe operator probe policy and endpoint identity controls.

The policy is an explicit authorization boundary for callers wiring probe
executors. It never accepts arbitrary URLs from a client request.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tusker_gateway.protocol_runtime import endpoint_identity


@dataclass(frozen=True)
class ProbeTarget:
    provider: str
    model: str
    endpoint_id: str


def authorize_probe_target(
    *, provider: str, model: str, endpoint: dict[str, Any],
    approved: bool, configured_endpoints: list[dict[str, Any]],
) -> ProbeTarget:
    """Require a configured endpoint and explicit approval.

    Caller must also enforce admin authentication and use the selected
    endpoint's existing auth strategy. This function is not an auth service.
    """
    if not approved:
        raise PermissionError("operator approval is required")
    if not provider or not model:
        raise ValueError("provider and model are required")
    identity = endpoint_identity(endpoint)
    if not any(
        endpoint_identity(configured) == identity
        and configured.get("base_url") == endpoint.get("base_url")
        and configured.get("chat_path") == endpoint.get("chat_path")
        for configured in configured_endpoints
    ):
        raise PermissionError("unconfigured probe endpoint")
    return ProbeTarget(provider=provider, model=model, endpoint_id=identity)
