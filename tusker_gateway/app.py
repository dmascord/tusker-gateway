"""Application factory: assemble aiohttp app from modules."""

import os

import aiohttp
from aiohttp import web

from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.budget import BudgetTracker, load_budget_config_from_env
from tusker_gateway.cache import ResponseCache, load_cache_config_from_env
from tusker_gateway.circuit_breaker import CircuitBreaker, load_circuit_config_from_env
from tusker_gateway.config import load_config
from tusker_gateway.dashboard import (
    dashboard_breakers,
    dashboard_cooldowns,
    dashboard_handler,
    dashboard_meta,
    dashboard_pools,
    dashboard_quality,
    dashboard_quota,
)
from tusker_gateway.endpoints import (
    chat_completions_handler,
    metrics_handler,
    models_handler,
    responses_handler,
)
from tusker_gateway.errors import GatewayError, openai_error
from tusker_gateway.health import health_handler, ready_handler, status_handler
from tusker_gateway.metrics import MetricsRegistry
from tusker_gateway.rate_limit import RateLimiter, load_rate_limit_config_from_env
from tusker_gateway.tracing import Tracer, load_tracer_config_from_env


def create_app() -> web.Application:
    """Build and return the aiohttp Application."""
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app["config"] = load_config()

    # Release 1 capabilities: cache, budget, metrics. All default-disabled
    # via env so existing deployments are unaffected.
    metrics = MetricsRegistry()
    metrics.add_meta("version", "0.1.0")
    app["metrics"] = metrics

    cache_cfg = load_cache_config_from_env()
    app["cache"] = ResponseCache(cache_cfg)

    budget_cfg = load_budget_config_from_env()
    app["budget"] = BudgetTracker(budget_cfg)

    # Release 2 capabilities: circuit breaker, rate limiter, OTLP tracing,
    # dashboard. All default-disabled via env.
    breaker_cfg = load_circuit_config_from_env()
    app["breaker"] = CircuitBreaker(breaker_cfg)

    ratelimit_cfg = load_rate_limit_config_from_env()
    app["ratelimit"] = RateLimiter(ratelimit_cfg)

    tracer_cfg = load_tracer_config_from_env()
    app["tracer"] = Tracer(tracer_cfg)

    async def on_startup(app):
        config = app["config"]
        app["http_session"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
        )
        # Hydrate persistent cooldowns into the in-memory tracker
        try:
            from pathlib import Path as _P
            from tusker_gateway.cooldown import global_tracker
            from tusker_gateway.persistent_cooldown import PersistentCooldownStore
            db_dir = _P(config.get("quality_db_path", "data/quality.db")).parent
            store = PersistentCooldownStore(db_path=db_dir / "cooldowns.db")
            store.hydrate(global_tracker())
            store.hydrate_providers(global_tracker())
            store.purge_expired()
        except Exception:
            pass  # best-effort
        # Start OTLP trace flusher.
        tracer = app.get("tracer")
        if tracer is not None and getattr(tracer, "enabled", False):
            await tracer.start()

    async def on_cleanup(app):
        tracer = app.get("tracer")
        if tracer is not None and getattr(tracer, "enabled", False):
            await tracer.stop()
        if "http_session" in app:
            await app["http_session"].close()

    auth = AuthMiddleware()
    metrics_token = os.environ.get("TUSKER_METRICS_TOKEN", "").strip()

    @web.middleware
    async def auth_middleware(request, handler):
        if request.path in ("/health", "/ready"):
            return await handler(request)
        # /metrics + /dashboard are opt-in authenticated.
        if request.path in ("/metrics", "/dashboard") or request.path.startswith("/dashboard/"):
            if metrics_token:
                token = request.headers.get("X-Tusker-Metrics-Token", "").strip()
                if not _secrets_compare(token, metrics_token):
                    return web.json_response(
                        openai_error("metrics token required", code="invalid_api_key", error_type="invalid_request_error"),
                        status=401,
                    )
            return await handler(request)
        try:
            await auth.verify(request)
        except GatewayError as exc:
            return web.json_response(
                openai_error(exc.message, code=exc.code, error_type=exc.error_type),
                status=exc.status,
            )
        return await handler(request)

    app.middlewares.append(auth_middleware)

    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)
    app.router.add_get("/status", status_handler)
    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/dashboard", dashboard_handler)
    app.router.add_get("/dashboard/partials/meta", dashboard_meta)
    app.router.add_get("/dashboard/partials/pools", dashboard_pools)
    app.router.add_get("/dashboard/partials/breakers", dashboard_breakers)
    app.router.add_get("/dashboard/partials/cooldowns", dashboard_cooldowns)
    app.router.add_get("/dashboard/partials/quota", dashboard_quota)
    app.router.add_get("/dashboard/partials/quality", dashboard_quality)
    app.router.add_get("/v1/models", models_handler)
    app.router.add_post("/v1/chat/completions", chat_completions_handler)
    app.router.add_post("/v1/responses", responses_handler)

    app.on_cleanup.append(on_cleanup)
    app.on_startup.append(on_startup)
    return app


def _secrets_compare(a: str, b: str) -> bool:
    import secrets
    if not a or not b:
        return False
    return secrets.compare_digest(a, b)


def main() -> None:
    config = load_config()
    app = create_app()
    web.run_app(app, host=config["host"], port=config["port"])


if __name__ == "__main__":
    main()