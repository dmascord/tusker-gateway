"""Health and readiness endpoints."""
from __future__ import annotations

from typing import Any

from aiohttp import web


def health_handler(request: web.Request) -> web.Response:
    """GET /health — liveness probe."""
    return web.json_response({"status": "ok", "version": "0.1.0"})


def ready_handler(request: web.Request) -> web.Response:
    """GET /ready — readiness probe.

    Readiness means the HTTP server is up and the app finished startup.
    We avoid re-loading configuration here so readiness reflects live state,
    not whether environment-only defaults happen to parse a second time.
    """
    if "config" not in request.app:
        return web.json_response({"status": "error", "reason": "config not loaded"}, status=503)
    cfg = request.app["config"]
    pools = list(cfg.get("pools", {}).keys())
    return web.json_response({"status": "ok", "pools": pools})


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
