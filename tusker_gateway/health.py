"""Health and readiness endpoints."""
from __future__ import annotations

import logging
import os
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

_GIT_COMMIT = os.environ.get("TUSKER_COMMIT", "unknown").strip()


def _normalise_provider_name(value: Any) -> str:
    """Return the provider spelling used by the runtime registry."""
    return str(value or "").strip().lower().replace("_", "-")


def _provider_kind(endpoint: Any) -> str:
    """Return an endpoint's auth kind for readiness credential checks."""
    if isinstance(endpoint, dict):
        return str(endpoint.get("kind", endpoint.get("auth_type", "bearer"))).lower()
    return str(
        getattr(endpoint, "kind", getattr(endpoint, "auth_type", "bearer"))
    ).lower()


def _credential_pool_sizes(request: web.Request, cfg: dict[str, Any]) -> dict[str, int]:
    """Return non-secret credential counts for OAuth/Codex providers."""
    sizes: dict[str, int] = {}

    rotators = request.app.get("credential_rotators")
    if isinstance(rotators, dict):
        for provider, rotator in rotators.items():
            try:
                sizes[_normalise_provider_name(provider)] = max(
                    0, int(getattr(rotator, "size", 0))
                )
            except (TypeError, ValueError):
                sizes[_normalise_provider_name(provider)] = 0

    configured = cfg.get("credential_pools")
    if isinstance(configured, dict):
        for provider, credentials in configured.items():
            name = _normalise_provider_name(provider)
            if name in sizes:
                continue
            sizes[name] = (
                sum(isinstance(credential, dict) for credential in credentials)
                if isinstance(credentials, list)
                else 0
            )

    # Preserve the legacy Codex configuration path for lightweight callers
    # that do not assemble credential_rotators during app startup.
    if "openai-codex" not in sizes:
        credentials = cfg.get("codex_credentials", [])
        sizes["openai-codex"] = (
            sum(isinstance(credential, dict) for credential in credentials)
            if isinstance(credentials, list)
            else 0
        )
    return sizes


def _pool_fallbacks(
    pool_name: str,
    pool: Any,
    pool_manager: Any | None,
) -> tuple[str, ...]:
    """Return the same direct fallback list request routing will use."""
    if pool_manager is not None:
        fallback_pools = getattr(pool_manager, "fallback_pools", None)
        if callable(fallback_pools):
            try:
                return tuple(fallback_pools(pool_name))
            except Exception:
                logger.debug(
                    "readiness fallback lookup failed for pool=%s",
                    pool_name,
                    exc_info=True,
                )
    return tuple(getattr(pool, "fallback_pools", ()) or ())


def _runtime_pool_candidates(
    request: web.Request,
    cfg: dict[str, Any],
    pool_name: str,
    pool: Any,
    provider_registry: dict[str, Any],
) -> list[Any] | None:
    """Return request-time candidates, or ``None`` for lightweight apps.

    The production app installs a shared PoolManager before it can serve
    traffic. Reusing it here keeps readiness aligned with disabled providers,
    missing bearer keys, catalog additions, and the live pool contents.
    """
    pool_manager = request.app.get("pool_manager")
    model_map = getattr(pool_manager, "models", None)
    if not isinstance(model_map, dict):
        return None

    credential_sizes = _credential_pool_sizes(request, cfg)
    candidates: list[Any] = []
    for spec in model_map.get(pool_name, ()):
        provider = _normalise_provider_name(getattr(spec, "provider", ""))
        endpoint = provider_registry.get(provider) or provider_registry.get(
            getattr(spec, "provider", ""),
        )
        if endpoint is None:
            continue
        if bool(getattr(pool, "zdr", False)) and not bool(
            getattr(spec, "zdr_ok", False)
        ):
            continue
        if _provider_kind(endpoint) in {"oauth", "codex"} and not credential_sizes.get(
            provider, 0
        ):
            continue
        candidates.append(spec)
    return candidates


def health_handler(request: web.Request) -> web.Response:
    """GET /health — liveness probe."""
    logger.debug('health check')
    from tusker_gateway.rtk import is_enabled

    semantic_cache = request.app.get("semantic_cache")
    return web.json_response({
        "status": "ok",
        "version": "0.1.0",
        "commit": _GIT_COMMIT,
        "rtk_enabled": request.app.get("rtk_enabled", is_enabled()),
        "semantic_cache_enabled": bool(
            semantic_cache is not None and semantic_cache.enabled
        ),
    })


def ready_handler(request: web.Request) -> web.Response:
    """GET /ready — readiness probe.

    Returns 503 if:
    - config is not loaded
    - the primary ``code`` pool and its explicit fallbacks have no usable
      candidates

    Optional pools may be empty without taking the whole gateway out of
    service. The response reports those pools as degraded for diagnosis.
    """
    logger.debug('ready check')
    if "config" not in request.app:
        logger.warning('readiness failed: config not loaded')
        return web.json_response({"status": "error", "reason": "config not loaded"}, status=503)
    cfg = request.app["config"]
    pools = cfg.get("pools", {})
    if not pools:
        logger.warning('readiness failed: no pools configured')
        return web.json_response({"status": "error", "reason": "no pools configured"}, status=503)

    # Validate that every pool has at least one candidate whose provider is known.
    from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
    from tusker_gateway.pools import ModelSpec

    provider_registry = cfg.get("providers")
    if not isinstance(provider_registry, dict) or not provider_registry:
        provider_registry = DEFAULT_PROVIDER_REGISTRY

    pool_health: dict[str, Any] = {}
    pool_manager = request.app.get("pool_manager")
    empty_pools: list[str] = []
    for name, pool in pools.items():
        candidates = []
        for m in pool.models:
            try:
                endpoint = provider_registry.get(m.get("provider", ""))
                if isinstance(endpoint, dict):
                    provider_zdr_ok = bool(endpoint.get("zdr_ok", False))
                else:
                    provider_zdr_ok = bool(
                        getattr(endpoint, "zdr_ok", False)
                    ) if endpoint is not None else False
                spec = ModelSpec.from_dict(
                    m,
                    default_window=pool.context_window,
                    zdr=pool.zdr,
                    provider_zdr_ok=provider_zdr_ok,
                )
                candidates.append(spec)
            except Exception as exc:
                pool_health.setdefault(name, {"errors": []})["errors"].append(str(exc))
        valid = [s for s in candidates if s.provider in provider_registry]
        policy_eligible = [s for s in valid if not pool.zdr or s.zdr_ok]
        runtime_candidates = _runtime_pool_candidates(
            request,
            cfg,
            name,
            pool,
            provider_registry,
        )
        # Lightweight test apps may expose only config and intentionally do
        # not build a PoolManager. Keep their historical static validation;
        # the production app always takes the runtime path above.
        usable_count = (
            len(runtime_candidates)
            if runtime_candidates is not None
            else len(policy_eligible)
        )
        invalid_count = len(candidates) - len(valid)
        pool_health[name] = {
            "total": len(candidates),
            "valid": len(valid),
            "policy_eligible": len(policy_eligible),
            "usable": usable_count,
            "invalid": invalid_count,
            "invalid_entries": [
                {"provider": s.provider, "model": s.model}
                for s in candidates
                if s.provider not in provider_registry
            ],
        }
        if usable_count == 0:
            empty_pools.append(name)

    primary_pool = "code" if "code" in pools else next(iter(pools), None)
    primary_route = (
        [primary_pool, *_pool_fallbacks(primary_pool, pools[primary_pool], pool_manager)]
        if primary_pool is not None
        else []
    )
    primary_route = [name for name in primary_route if name in pool_health]
    primary_usable = any(pool_health[name]["usable"] > 0 for name in primary_route)

    if not primary_usable:
        logger.warning(
            "readiness failed: primary route has no usable candidates "
            "primary=%s route=%s empty_pools=%s",
            primary_pool,
            primary_route,
            empty_pools,
        )
        return web.json_response(
            {
                "status": "error",
                "reason": "no usable candidates for primary route",
                "primary_pool": primary_pool,
                "primary_route": primary_route,
                "empty_pools": empty_pools,
                "pools": pool_health,
            },
            status=503,
        )

    return web.json_response(
        {
            "status": "ok",
            "primary_pool": primary_pool,
            "primary_route": primary_route,
            "degraded_pools": empty_pools,
            "pools": pool_health,
        }
    )


def status_handler(request: web.Request) -> web.Response:
    """GET /status — detailed runtime status."""
    from tusker_gateway.config import load_config
    from tusker_gateway.provider_usage import (
        ProviderUsageDB,
        capacity_controller,
        default_provider_usage_db_path,
    )
    from tusker_gateway.quality import QualityDB
    from tusker_gateway.pools import PoolManager
    from tusker_gateway.rtk import is_enabled
    from tusker_gateway.cooldown import global_tracker
    from tusker_gateway.model_capability import ModelCapabilityDB

    config = load_config()
    quality = QualityDB(config["quality_db_path"])
    model_capabilities = request.app.get("model_capabilities") or ModelCapabilityDB(
        config.get("model_capability_db_path", "data/model_capability.db")
    )
    provider_usage = ProviderUsageDB(
        default_provider_usage_db_path(config["quality_db_path"])
    )
    # Reuse the live manager so catalog additions, capability records, and
    # pool rebuilds shown here match request-time selection.
    pools = request.app.get("pool_manager") or PoolManager(config)

    try:
        from tusker_gateway.persistent_cooldown import PersistentCooldownStore
        from pathlib import Path
        db_path = Path(config.get("quality_db_path", "data/quality.db")).parent / "cooldowns.db"
        store = PersistentCooldownStore(db_path=db_path)
        purged = store.purge_expired()
    except Exception:
        purged = 0

    status: dict[str, Any] = {
        "status": "ok",
        "version": "0.1.0",
        "pools": pools.status(),
        "catalog": (
            request.app["catalog_registry"].diagnostics()
            if request.app.get("catalog_registry") is not None
            else {}
        ),
        "quality": quality.status(),
        "model_capabilities": model_capabilities.status(),
        "provider_usage": provider_usage.status(),
        "provider_capacity": capacity_controller().snapshot(),
        "cooldowns": global_tracker().snapshot(),
        "purged_cooldowns": purged,
        "rtk_enabled": request.app.get("rtk_enabled", is_enabled()),
    }
    qualification_task = request.app.get("qualification_task")
    status["qualification_maintenance"] = {
        "enabled": qualification_task is not None,
        "running": bool(qualification_task is not None and not qualification_task.done()),
    }
    semantic_cache = request.app.get("semantic_cache")
    status["semantic_cache"] = {
        "enabled": bool(semantic_cache is not None and semantic_cache.enabled),
        "stats": (
            semantic_cache.stats_snapshot()
            if semantic_cache is not None
            else {}
        ),
    }
    identity_store = request.app.get("identity_store")
    identity_config = getattr(identity_store, "config", None)
    audit = request.app.get("audit")
    audit_config = getattr(audit, "config", None)
    deadline_config = request.app.get("deadline_config")
    idempotency = request.app.get("idempotency")
    idempotency_config = getattr(idempotency, "config", None)
    status["enterprise_controls"] = {
        "identity_profiles": len(getattr(identity_config, "identities", {})),
        "identity_required": bool(getattr(identity_config, "required", False)),
        "audit_enabled": bool(getattr(audit_config, "enabled", False)),
        "audit_integrity": (
            "hmac-sha256"
            if getattr(audit_config, "hmac_key", "")
            else "sha256" if getattr(audit_config, "enabled", False) else "disabled"
        ),
        "audit_fail_closed": bool(getattr(audit_config, "fail_closed", False)),
        "request_timeout_ms": getattr(deadline_config, "default_timeout_ms", 0),
        "max_request_timeout_ms": getattr(deadline_config, "max_timeout_ms", 0),
        "idempotency_enabled": bool(getattr(idempotency_config, "enabled", False)),
        "idempotency_ttl_secs": getattr(idempotency_config, "ttl_secs", 0),
    }
    return web.json_response(status)
