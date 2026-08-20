"""Health and readiness endpoints."""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


def health_handler(request: web.Request) -> web.Response:
    """GET /health — liveness probe."""
    return web.json_response({"status": "ok", "version": "0.1.0"})


def ready_handler(request: web.Request) -> web.Response:
    """GET /ready — readiness probe.

    Returns 503 if:
    - config is not loaded
    - any pool has no valid candidates (all entries reference unknown providers)

    Returns 200 with pool health summary otherwise.
    """
    if "config" not in request.app:
        return web.json_response({"status": "error", "reason": "config not loaded"}, status=503)
    cfg = request.app["config"]
    pools = cfg.get("pools", {})
    if not pools:
        return web.json_response({"status": "error", "reason": "no pools configured"}, status=503)

    # Validate that every pool has at least one candidate whose provider is known.
    from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
    from tusker_gateway.pools import ModelSpec

    pool_health: dict[str, Any] = {}
    empty_pools: list[str] = []
    for name, pool in pools.items():
        candidates = []
        for m in pool.models:
            try:
                spec = ModelSpec.from_dict(m, default_window=pool.context_window, zdr=pool.zdr)
                candidates.append(spec)
            except Exception as exc:
                pool_health.setdefault(name, {"errors": []})["errors"].append(str(exc))
        valid = [s for s in candidates if s.provider in DEFAULT_PROVIDER_REGISTRY]
        invalid_count = len(candidates) - len(valid)
        pool_health[name] = {
            "total": len(candidates),
            "valid": len(valid),
            "invalid": invalid_count,
            "invalid_entries": [
                {"provider": s.provider, "model": s.model}
                for s in candidates
                if s.provider not in DEFAULT_PROVIDER_REGISTRY
            ],
        }
        if not valid:
            empty_pools.append(name)

    if empty_pools:
        return web.json_response(
            {
                "status": "error",
                "reason": "pools with no valid candidates",
                "empty_pools": empty_pools,
                "pools": pool_health,
            },
            status=503,
        )

    return web.json_response({"status": "ok", "pools": pool_health})


def status_handler(request: web.Request) -> web.Response:
    """GET /status — detailed runtime status."""
    from tusker_gateway.config import load_config
    from tusker_gateway.quality import QualityDB
    from tusker_gateway.pools import PoolManager

    config = load_config()
    quality = QualityDB(config["quality_db_path"])
    pools = PoolManager(config)

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
        "quality": quality.status(),
        "purged_cooldowns": purged,
    }
    return web.json_response(status)
