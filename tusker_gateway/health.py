"""Health and readiness endpoints."""
from __future__ import annotations

from typing import Any

from aiohttp import web


def health_handler(request: web.Request) -> web.Response:
    """GET /health — liveness probe."""
    return web.json_response({"status": "ok", "version": "0.1.0"})


def ready_handler(request: web.Request) -> web.Response:
    """GET /ready — readiness probe.

    Readiness means:
    - config loaded
    - quality DB reachable
    - at least one provider pool has a healthy model
    """
    from tusker_gateway.copilot_enroll import load_auth_file
    creds = load_auth_file()
    if not creds:
        return web.json_response({"status": "error", "reason": "no credentials"}, status=503)
    return web.json_response({"status": "ok", "credential_count": len(creds)})


def status_handler(request: web.Request) -> web.Response:
    """GET /status — detailed runtime status."""
    from tusker_gateway.config import load_config
    from tusker_gateway.quality import QualityDB
    from tusker_gateway.pools import PoolManager

    config = load_config()
    quality = QualityDB(config["quality_db_path"])
    pools = PoolManager(config)

    status: dict[str, Any] = {
        "status": "ok",
        "version": "0.1.0",
        "pools": pools.status(),
        "quality": quality.status(),
    }
    return web.json_response(status)
