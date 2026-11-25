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

Two kinds of rows are reported:

- **providers** — concrete providers the gateway can dispatch to.
- **capacity groups** — shared concurrency budgets (``cli`` covers both CLI
  worker providers, ``local-llm`` the local backend). A group quarantine
  blocks every provider behind it, so it is reported separately and also
  marks those providers as not ready.
"""
from __future__ import annotations

import time
from typing import Any

from aiohttp import web

from tusker_gateway.cooldown import global_tracker
from tusker_gateway.provider_usage import (
    CLI_CAPACITY_GROUP,
    LOCAL_LLM_CAPACITY_GROUP,
    ProviderUsageDB,
    capacity_controller,
    capacity_group_for_route,
    default_provider_usage_db_path,
)

# Capacity groups that are *only* group names. Every other group name is also
# a concrete provider (``mlx-mac``, ``synthetic``, ``ollama-cloud``,
# ``nvidia``), so those names belong to the provider rows.
_GROUP_ONLY_NAMES = frozenset({
    CLI_CAPACITY_GROUP,
    LOCAL_LLM_CAPACITY_GROUP,
})


def _is_provider_key(name: str, known_providers: set[str]) -> bool:
    """Whether a usage/capacity key names a provider rather than a group."""
    return name in known_providers or name not in _GROUP_ONLY_NAMES


def _provider_summary(
    provider: str,
    *,
    cooldown_seconds: list[float],
    capacity: dict[str, Any] | None,
    usage: dict[str, Any] | None,
    group_cooldowns: dict[str, list[float]],
    groups: dict[str, list[str]],
) -> dict[str, Any]:
    """Build one provider's health row from already-collected state."""
    active = [s for s in cooldown_seconds if s > 0]
    # A provider-wide cooldown is reported once with model=""; count distinct
    # cooling routes rather than every model that inherits the same window.
    cooling = len({round(s, 1) for s in active})
    next_reset = min(active) if active else 0.0

    # A shared capacity group under quarantine blocks this provider's routes
    # even though the provider itself has no cooldown of its own.
    group_cooling = 0
    group_reset = 0.0
    for group in groups.get(provider, ()):
        windows = [s for s in group_cooldowns.get(group, ()) if s > 0]
        if not windows:
            continue
        group_cooling += len({round(s, 1) for s in windows})
        group_reset = min(group_reset, min(windows)) if group_reset else min(windows)

    requests = int((usage or {}).get("requests") or 0)
    successes = int((usage or {}).get("successes") or 0)
    failures = int((usage or {}).get("failures") or 0)
    success_rate = (successes / requests) if requests else None

    row: dict[str, Any] = {
        "provider": provider,
        "ready": cooling == 0 and group_cooling == 0,
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
    if groups.get(provider):
        row["capacity_groups"] = sorted(set(groups[provider]))
    if group_cooling:
        row["group_cooling_routes"] = group_cooling
        row["group_next_reset_seconds"] = round(group_reset, 1)
    return row


def _group_summary(
    group: str,
    *,
    cooldown_seconds: list[float],
    capacity: dict[str, Any] | None,
    usage: dict[str, Any] | None,
    providers: list[str],
) -> dict[str, Any]:
    """Build one capacity-group row from already-collected state."""
    active = [s for s in cooldown_seconds if s > 0]
    cooling = len({round(s, 1) for s in active})
    requests = int((usage or {}).get("requests") or 0)
    successes = int((usage or {}).get("successes") or 0)
    failures = int((usage or {}).get("failures") or 0)
    row: dict[str, Any] = {
        "group": group,
        "ready": cooling == 0,
        "cooling_routes": cooling,
        "next_reset_seconds": round(min(active), 1) if active else 0,
        "requests_today": requests,
        "failures_today": failures,
        "success_rate_today": (
            round(successes / requests, 4) if requests else None
        ),
        "providers": providers,
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
    providers: set[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Assemble the bounded provider-health payload.

    Pure function over already-collected snapshots so it is testable without
    a live app, cooldown tracker, or database.

    ``providers`` is the set of concrete provider names the gateway knows
    about. It separates real providers from the shared capacity groups that
    the usage ledger records against (for example ``opencode-cli`` and
    ``claude-code-cli`` share the ``cli`` group).
    """
    now = time.time() if now is None else now
    known_providers = {
        str(name).strip().lower() for name in (providers or ()) if str(name).strip()
    }

    by_provider: dict[str, list[float]] = {}
    provider_models: dict[str, set[str]] = {}
    group_cooldowns: dict[str, list[float]] = {}

    for entry in cooldowns.get("models") or []:
        provider = str(entry.get("provider") or "").strip().lower()
        if not provider:
            continue
        seconds = float(entry.get("seconds_remaining") or 0.0)
        by_provider.setdefault(provider, []).append(seconds)
        model = str(entry.get("model") or "").strip().lower()
        if model:
            provider_models.setdefault(provider, set()).add(model)

    for entry in cooldowns.get("providers") or []:
        provider = str(entry.get("provider") or "").strip().lower()
        if not provider:
            continue
        seconds = float(entry.get("seconds_remaining") or 0.0)
        by_provider.setdefault(provider, []).append(seconds)

    for entry in cooldowns.get("groups") or []:
        group = str(entry.get("group") or "").strip().lower()
        if not group:
            continue
        seconds = float(entry.get("seconds_remaining") or 0.0)
        group_cooldowns.setdefault(group, []).append(seconds)

    # Which capacity group does each provider share? A group quarantine has
    # to mark every provider behind it as not ready.
    groups: dict[str, list[str]] = {}
    group_providers: dict[str, set[str]] = {}
    for provider in known_providers | set(by_provider):
        group = capacity_group_for_route(provider, "")
        if not group:
            continue
        groups.setdefault(provider, []).append(group)
        group_providers.setdefault(group, set()).add(provider)
    for provider, models in provider_models.items():
        for model in models:
            group = capacity_group_for_route(provider, model)
            if not group:
                continue
            groups.setdefault(provider, []).append(group)
            group_providers.setdefault(group, set()).add(provider)

    # Usage rows and capacity counters are keyed by the logical group when
    # the route belongs to one, and by the provider otherwise.
    provider_usage: dict[str, dict[str, Any]] = {}
    group_usage: dict[str, dict[str, Any]] = {}
    provider_capacity: dict[str, dict[str, Any]] = {}
    group_capacity: dict[str, dict[str, Any]] = {}
    for key, values in usage_groups.items():
        name = str(key).strip().lower()
        if _is_provider_key(name, known_providers):
            provider_usage[name] = values
        else:
            group_usage[name] = values
    for key, values in capacity.items():
        name = str(key).strip().lower()
        if _is_provider_key(name, known_providers):
            provider_capacity[name] = values
        else:
            group_capacity[name] = values

    provider_names = (
        known_providers
        | set(by_provider)
        | set(provider_usage)
        | set(provider_capacity)
    )
    # A provider whose only capacity counter is keyed by a group name (the
    # CLI workers share ``cli``) still needs a row.
    for group, members in group_providers.items():
        provider_names.update(members)

    group_names = (
        set(group_cooldowns)
        | set(group_usage)
        | set(group_capacity)
        | set(group_providers)
    )

    provider_rows = [
        _provider_summary(
            provider,
            cooldown_seconds=by_provider.get(provider, []),
            capacity=provider_capacity.get(provider),
            usage=provider_usage.get(provider),
            group_cooldowns=group_cooldowns,
            groups=groups,
        )
        for provider in sorted(provider_names)
    ]

    group_rows = [
        _group_summary(
            group,
            cooldown_seconds=group_cooldowns.get(group, []),
            capacity=group_capacity.get(group),
            usage=group_usage.get(group),
            providers=sorted(group_providers.get(group, set())),
        )
        for group in sorted(group_names)
    ]

    ready = sum(1 for row in provider_rows if row["ready"])
    return {
        "generated_at": int(now),
        "summary": {
            "providers": len(provider_rows),
            "ready": ready,
            "degraded": len(provider_rows) - ready,
        },
        "providers": provider_rows,
        "capacity_groups": group_rows,
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
        known_providers = set(config.get("providers") or {})
    except Exception:  # noqa: BLE001 - health must never fail the request
        usage_groups = {}
        known_providers = set()

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
        providers=known_providers,
    )
    return web.json_response(payload, headers={"Cache-Control": "no-store"})


__all__ = ["provider_health_handler", "provider_health_payload"]
