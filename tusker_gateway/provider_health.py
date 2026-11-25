"""Provider-health reporting for clients.

The gateway is the only component that knows how healthy a *pool* is: which
credentials are cooling down, which provider groups are saturated, and how
many requests actually succeeded today. A client (OMP, a statusline script, a
dashboard) cannot derive any of that from a chat response.

``GET /v1/provider-health`` exposes that state in one bounded, secret-free
payload. It is deliberately a *router* view, not a vendor-quota view: the
gateway fronts many providers and pools several credentials per provider, so
``used_percent`` here means "how much of this pool is currently unavailable",
never "what percentage of an upstream quota has been consumed".

That distinction matters. ``x-codex-primary-used-percent`` describes one Codex
seat; this endpoint describes all of them. Reporting a single seat's quota as
the pool's would be wrong exactly when the pool is unhealthy, so this endpoint
does not claim to be that number.
"""
from __future__ import annotations

import time
from typing import Any

from aiohttp import web

from tusker_gateway.cooldown import global_tracker
from tusker_gateway.provider_usage import (
    ProviderUsageDB,
    capacity_controller,
    default_provider_usage_db_path,
)

# Cooldowns beyond this are treated as "unavailable" for the percentage
# calculation. A credential parked on a multi-day cooldown is not 40% used;
# it is out of service.
_LONG_COOLDOWN_SECS = 3600.0


def _provider_summary(
    provider: str,
    *,
    cooldown_seconds: list[float],
    capacity: dict[str, Any] | None,
    usage: dict[str, Any] | None,
    now: float,
) -> dict[str, Any]:
    """Build one provider's health row from already-collected state."""
    active = [s for s in cooldown_seconds if s > 0]
    # A provider-wide cooldown is reported once with model=""; count distinct
    # cooling routes rather than every model that inherits the same window.
    cooling = len({round(s, 1) for s in active})
    next_reset = min(active) if active else 0.0

    requests = int((usage or {}).get("requests") or 0)
    successes = int((usage or {}).get("successes") or 0)
    failures = int((usage or {}).get("failures") or 0)
    success_rate = (successes / requests) if requests else None

    row: dict[str, Any] = {
        "provider": provider,
        "ready": cooling == 0,
        "cooling_routes": cooling,
        "next_reset_seconds": round(next_reset, 1) if next_reset else 0,
        "requests_today": requests,
        "failures_today": failures,
        "success_rate_today": (
            round(success_rate, 4) if success_rate is not None else None
        ),
    }
    if capacity:
        row["capacity"] = {
            "active": int(capacity.get("active") or 0),
            "limit": int(capacity.get("limit") or 0),
            "queued": int(capacity.get("queued") or 0),
            "rejections": int(capacity.get("rejections") or 0),
        }
    return row


def provider_health_payload(
    *,
    cooldowns: dict[str, Any],
    capacity: dict[str, dict[str, Any]],
    usage_groups: dict[str, dict[str, Any]],
    now: float | None = None,
) -> dict[str, Any]:
    """Assemble the bounded provider-health payload.

    Pure function over already-collected snapshots so it is testable without
    a live app, cooldown tracker, or database.
    """
    now = time.time() if now is None else now
    by_provider: dict[str, list[float]] = {}

    for entry in cooldowns.get("models") or []:
        provider = str(entry.get("provider") or "").strip().lower()
        if not provider:
            continue
        seconds = float(entry.get("seconds_remaining") or 0.0)
        by_provider.setdefault(provider, []).append(seconds)

    for entry in cooldowns.get("providers") or []:
        provider = str(entry.get("provider") or "").strip().lower()
        if not provider:
            continue
        seconds = float(entry.get("seconds_remaining") or 0.0)
        by_provider.setdefault(provider, []).append(seconds)

    # A provider with usage or capacity data but no cooldowns is healthy and
    # must still appear: absence of a cooldown is the signal.
    for provider in list(usage_groups) + list(capacity):
        by_provider.setdefault(str(provider).strip().lower(), [])

    providers = [
        _provider_summary(
            provider,
            cooldown_seconds=seconds,
            capacity=capacity.get(provider),
            usage=usage_groups.get(provider),
            now=now,
        )
        for provider, seconds in sorted(by_provider.items())
    ]

    ready = sum(1 for row in providers if row["ready"])
    return {
        "generated_at": int(now),
        "summary": {
            "providers": len(providers),
            "ready": ready,
            "degraded": len(providers) - ready,
        },
        "providers": providers,
    }


async def provider_health_handler(request: web.Request) -> web.Response:
    """GET /v1/provider-health — router-level provider health.

    Authenticated by the gateway's normal Bearer middleware. Contains no
    credential material, no upstream URLs, and no request bodies.
    """
    try:
        from tusker_gateway.config import load_config

        config = load_config()
        usage_db = ProviderUsageDB(
            default_provider_usage_db_path(config["quality_db_path"])
        )
        usage_status = usage_db.status()
        usage_groups = usage_status.get("groups") or {}
    except Exception:  # noqa: BLE001 - health must never fail the request
        usage_groups = {}

    try:
        capacity = capacity_controller().snapshot()
    except Exception:  # noqa: BLE001
        capacity = {}

    try:
        cooldowns = global_tracker().snapshot()
    except Exception:  # noqa: BLE001
        cooldowns = {}

    payload = provider_health_payload(
        cooldowns=cooldowns,
        capacity=capacity,
        usage_groups=usage_groups,
    )
    return web.json_response(payload, headers={"Cache-Control": "no-store"})


__all__ = ["provider_health_handler", "provider_health_payload"]
