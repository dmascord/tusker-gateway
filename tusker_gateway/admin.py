"""Authenticated read-only admin API for Tusker Gateway operators.

All endpoints require a valid API key (``Authorization: Bearer <key>``).
The caller's identity is resolved through the existing identity store, so
legacy keys (no identity profile) are accepted — they get full admin access
as they would for ``/status``.  Operators should treat admin API output as
operational data; no raw credentials are ever returned.

Routes
-------
GET  /admin/diagnostics   — aggregated view of every subsystem
GET  /admin/providers    — configured providers, base URLs, auth kind, key status
GET  /admin/pools        — pool model lists, validity counts, auto-catalog sources
GET  /admin/catalog      — per-provider catalog refresh state
GET  /admin/cooldowns    — active per-model/provider cooldowns
GET  /admin/breakers    — circuit breaker states
GET  /admin/keys         — API key fingerprints, principals, and identity metadata
GET  /admin/usage        — per-provider usage counters and capacity state

Response format
---------------
All endpoints return JSON. Errors use the OpenAI error shape::

    {"error": {"message": "...", "code": "...", "type": "..."}}
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from aiohttp import web

from tusker_gateway.identity import fingerprint_api_key
from tusker_gateway.storage import storage_status

logger = logging.getLogger(__name__)

# ─── Route handlers ────────────────────────────────────────────────────────────


async def admin_diagnostics(request: web.Request) -> web.Response:
    """GET /admin/diagnostics — aggregated view of every subsystem.

    Returns the same fields as ``GET /status`` plus the per-provider catalog
    refresh state and the circuit-breaker snapshot.
    """
    from tusker_gateway.config import load_config
    from tusker_gateway.cooldown import global_tracker
    from tusker_gateway.circuit_breaker import CircuitBreaker
    from tusker_gateway.rate_limit import RateLimiter
    from tusker_gateway.provider_usage import (
        ProviderUsageDB,
        capacity_controller,
        default_provider_usage_db_path,
    )
    from tusker_gateway.quality import QualityDB
    from tusker_gateway.pools import PoolManager

    config = load_config()
    pools = request.app.get("pool_manager") or PoolManager(config)
    quality = QualityDB(config["quality_db_path"])
    provider_usage = ProviderUsageDB(default_provider_usage_db_path(config["quality_db_path"]))
    breaker: CircuitBreaker | None = request.app.get("breaker")
    ratelimit: RateLimiter | None = request.app.get("ratelimit")
    catalog_reg = request.app.get("catalog_registry")

    try:
        from tusker_gateway.persistent_cooldown import PersistentCooldownStore
        from pathlib import Path
        db_path = Path(config.get("quality_db_path", "data/quality.db")).parent / "cooldowns.db"
        store = PersistentCooldownStore(db_path=db_path)
        store.purge_expired()
    except Exception:
        pass  # non-fatal; global_tracker snapshot will reflect in-memory state

    result: dict[str, Any] = {
        "pools": pools.status(),
        "catalog": catalog_reg.diagnostics() if catalog_reg else {},
        "quality": quality.status(),
        "provider_usage": provider_usage.status(),
        "provider_capacity": capacity_controller().snapshot(),
        "cooldowns": global_tracker().snapshot(),
        "circuit_breakers": breaker.snapshot() if breaker else {},
        "rate_limiter_stats": ratelimit.stats_snapshot() if ratelimit else {},
        "state_store": storage_status(),
    }

    identity_store = request.app.get("identity_store")
    if identity_store is not None:
        identity_cfg = getattr(identity_store, "config", None)
        result["enterprise_controls"] = {
            "identity_profiles": len(getattr(identity_cfg, "identities", {})),
            "identity_required": bool(getattr(identity_cfg, "required", False)),
        }

    return web.json_response(result)


async def admin_providers(request: web.Request) -> web.Response:
    """GET /admin/providers — configured providers with base URLs and auth status.

    Raw API keys are never returned.  Each provider shows whether a key is
    configured and, for bearer-key providers, a SHA-256 fingerprint of the
    configured key so operators can correlate without exposing the secret.
    """
    config = request.app.get("config", {})
    provider_registry: dict[str, Any] = config.get("providers", {})
    provider_keys: dict[str, str | None] = config.get("provider_api_keys", {})
    excluded: set[str] = {p.lower() for p in config.get("excluded_providers", [])}
    disabled: set[str] = {p.lower() for p in config.get("disabled_providers", [])}

    result: dict[str, Any] = {}
    for name, prov in provider_registry.items():
        name_lower = str(name).lower()
        raw_key = provider_keys.get(name_lower)
        result[str(name)] = {
            "name": str(name),
            "base_url": getattr(prov, "base_url", None),
            "chat_path": getattr(prov, "chat_path", None),
            "auth_kind": getattr(prov, "auth_type", None) or getattr(prov, "kind", "bearer"),
            "has_key": bool(raw_key),
            "key_fingerprint": (
                hashlib.sha256(raw_key.encode()).hexdigest()[:16]
                if raw_key else None
            ),
            "models_path": getattr(prov, "models_path", None),
            "rerank_path": getattr(prov, "rerank_path", None),
            "model_header": getattr(prov, "model_header", None),
            "zdr_ok": getattr(prov, "zdr_ok", False),
            "heavyweight": getattr(prov, "heavyweight", False),
            "excluded": name_lower in excluded,
            "disabled": name_lower in disabled,
        }

    return web.json_response({"providers": result})


async def admin_pools(request: web.Request) -> web.Response:
    """GET /admin/pools — current pool definitions and model lists."""
    from tusker_gateway.pools import PoolManager
    from tusker_gateway.config import load_config

    pools = request.app.get("pool_manager") or PoolManager(load_config())
    return web.json_response({"pools": pools.status()})


async def admin_catalog(request: web.Request) -> web.Response:
    """GET /admin/catalog — per-provider catalog refresh state and entry counts."""
    catalog_reg = request.app.get("catalog_registry")
    if catalog_reg is None:
        return web.json_response({"catalog": {}})

    diag = catalog_reg.diagnostics()
    # Strip error messages from disabled/error providers — those can contain
    # sensitive upstream details and are not useful for an admin overview.
    for provider, d in diag.items():
        if d.get("last_refresh_status") != "ok":
            d.pop("last_error", None)
            d.pop("last_error_class", None)

    return web.json_response({"catalog": diag})


async def admin_cooldowns(request: web.Request) -> web.Response:
    """GET /admin/cooldowns — active per-model/provider cooldowns."""
    from tusker_gateway.cooldown import global_tracker

    return web.json_response({"cooldowns": global_tracker().snapshot()})


async def admin_breakers(request: web.Request) -> web.Response:
    """GET /admin/breakers — circuit breaker states."""
    breaker = request.app.get("breaker")
    if breaker is None:
        return web.json_response({"breakers": {}})
    return web.json_response({"breakers": breaker.snapshot()})


async def admin_keys(request: web.Request) -> web.Response:
    """GET /admin/keys — API key fingerprints, principals, and identity metadata.

    Raw keys are never returned.  Each entry shows the SHA-256 fingerprint
    (stable, non-secret identifier), the resolved principal, tenant, and any
    allowlists configured for that key.
    """
    identity_store = request.app.get("identity_store")
    if identity_store is None:
        return web.json_response({"keys": []})

    identities: dict[str, Any] = {}
    for fp, identity in identity_store.config.identities.items():
        identities[fp] = {
            "fingerprint": fp,
            "principal": identity.principal,
            "tenant": identity.tenant,
            "scopes": list(identity.scopes),
            "allowed_pools": list(identity.allowed_pools),
            "allowed_models": list(identity.allowed_models),
            "allowed_providers": list(identity.allowed_providers),
            "managed": identity.managed,
        }

    return web.json_response({
        "keys": list(identities.values()),
        "total": len(identities),
        "strict_identity": identity_store.config.required,
    })


async def admin_usage(request: web.Request) -> web.Response:
    """GET /admin/usage — per-provider usage counters and capacity state."""
    from tusker_gateway.config import load_config
    from tusker_gateway.provider_usage import (
        ProviderUsageDB,
        capacity_controller,
        default_provider_usage_db_path,
    )
    from tusker_gateway.rate_limit import RateLimiter

    config = load_config()
    provider_usage = ProviderUsageDB(default_provider_usage_db_path(config["quality_db_path"]))
    ratelimit: RateLimiter | None = request.app.get("ratelimit")

    result: dict[str, Any] = {
        "provider_usage": provider_usage.status(),
        "provider_capacity": capacity_controller().snapshot(),
    }
    if ratelimit is not None:
        result["rate_limiter_stats"] = ratelimit.stats_snapshot()
        result["rate_limiter_buckets"] = ratelimit.snapshot()

    return web.json_response(result)


# ─── Module-level route table ──────────────────────────────────────────────────
# (Used by app.py to register routes; handler names must match above.)

ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/admin/diagnostics", "admin_diagnostics"),
    ("GET", "/admin/providers", "admin_providers"),
    ("GET", "/admin/pools", "admin_pools"),
    ("GET", "/admin/catalog", "admin_catalog"),
    ("GET", "/admin/cooldowns", "admin_cooldowns"),
    ("GET", "/admin/breakers", "admin_breakers"),
    ("GET", "/admin/keys", "admin_keys"),
    ("GET", "/admin/usage", "admin_usage"),
]
