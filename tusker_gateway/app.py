"""Application factory: assemble aiohttp app from modules."""

import asyncio
import logging
import os
from typing import Any

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
    dashboard_guardrails,
    dashboard_meta,
    dashboard_pools,
    dashboard_quality,
    dashboard_quota,
)
from tusker_gateway.endpoints import (
    chat_completions_handler,
    images_handler,
    metrics_handler,
    models_handler,
    responses_handler,
    tts_handler,
    video_handler,
 )
from tusker_gateway.anthropic_adapter import anthropic_messages_handler

from tusker_gateway.errors import GatewayError, openai_error
from tusker_gateway.health import health_handler, ready_handler, status_handler
from tusker_gateway.guardrails import init_guard_pipeline, load_guardrails_config_from_env
from tusker_gateway.metrics import MetricsRegistry
from tusker_gateway.rate_limit import RateLimiter, load_rate_limit_config_from_env
from tusker_gateway.tracing import Tracer, load_tracer_config_from_env
from tusker_gateway.providers.capabilities import (
    CapabilitiesRegistry,
    capabilities_refresh_loop,
)


def create_app() -> web.Application:
    """Build and return the aiohttp Application."""
    # Configure structured logging for the gateway.
    log_level = os.environ.get("TUSKER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    log = logging.getLogger("tusker_gateway.app")

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

    # Release 3: semantic cache (optional, default-disabled).
    try:
        from tusker_gateway.semantic_cache import SemanticCache, load_semantic_cache_config_from_env
        sem_cache_cfg = load_semantic_cache_config_from_env()
        app["semantic_cache"] = SemanticCache(sem_cache_cfg)
        if sem_cache_cfg.enabled:
            log.info(
                "semantic cache configured enabled=true similarity=%.2f model=%s revision=%s local_files_only=%s deterministic_only=%s excluded_pools=%s",
                sem_cache_cfg.similarity_threshold,
                sem_cache_cfg.model_name,
                sem_cache_cfg.model_revision or "unversioned",
                sem_cache_cfg.local_files_only,
                sem_cache_cfg.require_deterministic,
                ",".join(sem_cache_cfg.excluded_pools) or "none",
            )
        else:
            log.info("semantic cache disabled")
    except ImportError:
        log.info("semantic cache unavailable (missing deps: chromadb, sentence-transformers)")
        app["semantic_cache"] = None

    # Guardrails: default-disabled via env.
    guard_cfg = load_guardrails_config_from_env()
    app["guard_pipeline"] = init_guard_pipeline(guard_cfg)
    if guard_cfg.get("enabled", False):
        log.info("guardrails enabled (max_output=%s)", guard_cfg.get("max_output_tokens"))
    else:
        log.info("guardrails disabled")

    # Shared PoolManager so catalog refresh + session stickiness +
    # cooldown state are consistent across all request handlers.
    from tusker_gateway.pools import PoolManager
    app["pool_manager"] = PoolManager(app["config"])

    # Image generation handler (Phase: image/video generation support).
    from tusker_gateway.providers.image_generation import ImageGenerationHandler
    # Build one rotator per OAuth/Codex provider. These pools are deliberately
    # independent: a Copilot token must never be selected for chatgpt.com, and
    # a Codex credential must never be used to authenticate a Copilot catalog.
    from tusker_gateway.passthrough import CodexTokenRotator
    config = app["config"]
    credential_rotators: dict[str, CodexTokenRotator] = {}
    configured_pools = config.get("credential_pools")
    if isinstance(configured_pools, dict):
        for provider_name, credentials in configured_pools.items():
            provider = str(provider_name).lower()
            if provider not in {
                "openai-codex",
                "github-copilot",
                "github-copilot-enterprise",
            } or not isinstance(credentials, list):
                continue
            credential_rotators[provider] = CodexTokenRotator(
                [credential for credential in credentials if isinstance(credential, dict)],
                auth_file=(config.get("auth_file") if provider == "openai-codex" else None),
            )
    codex_rotator = credential_rotators.get("openai-codex")
    if codex_rotator is None:
        codex_rotator = CodexTokenRotator(
            config.get("codex_credentials") or [],
            auth_file=config.get("auth_file"),
        )
        credential_rotators["openai-codex"] = codex_rotator
    app["credential_rotators"] = credential_rotators
    app["codex_rotator"] = codex_rotator
    capability_registry = CapabilitiesRegistry(
        provider_keys=config.get("provider_api_keys", {}),
        codex_rotator=codex_rotator,
    )
    app["capability_registry"] = capability_registry
    app["image_handler"] = ImageGenerationHandler(
        app["config"], capability_registry=capability_registry
    )
    # TTS and video handlers (Phase: TTS/video support).
    from tusker_gateway.providers.tts import TTSHandler
    from tusker_gateway.providers.video import VideoHandler
    app["tts_handler"] = TTSHandler(
        app["config"], capability_registry=capability_registry
    )
    app["video_handler"] = VideoHandler(
        app["config"], capability_registry=capability_registry
    )


    async def on_startup(app):
        startup_log = logging.getLogger("tusker_gateway.startup")
        config = app["config"]
        app["http_session"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
        )
        for rotator in app.get("credential_rotators", {}).values():
            # The rotators are constructed before aiohttp startup so they can
            # also be passed to handlers during app assembly. Attach the
            # shared session before catalog/capability refresh begins.
            rotator._http = app["http_session"]
        startup_log.info("HTTP session created")
        # One stop signal controls every refresh loop using the shared session.
        stop_event = asyncio.Event()
        app["refresh_stop_event"] = stop_event
        # Hydrate persistent cooldowns into the in-memory tracker
        try:
            from pathlib import Path as _P
            from tusker_gateway.cooldown import global_tracker
            from tusker_gateway.persistent_cooldown import PersistentCooldownStore
            db_dir = _P(config.get("quality_db_path", "data/quality.db")).parent
            store = PersistentCooldownStore(db_path=db_dir / "cooldowns.db")
            loaded_models = store.hydrate(global_tracker())
            loaded_providers = store.hydrate_providers(global_tracker())
            purged = store.purge_expired()
            startup_log.info("cooldowns hydrated: %d model, %d provider cooldowns loaded, %d expired purged", loaded_models, loaded_providers, purged)
        except Exception as exc:
            startup_log.warning("cooldown hydration failed: %s", exc)
        # Start OTLP trace flusher.
        tracer = app.get("tracer")
        if tracer is not None and getattr(tracer, "enabled", False):
            await tracer.start()
            startup_log.info("OTLP tracer started (endpoint=%s)", tracer._config.endpoint)
        # Initialize semantic cache embedding model.
        sem_cache = app.get("semantic_cache")
        if sem_cache is not None and sem_cache.enabled:
            await sem_cache.initialize()
            startup_log.info(
                "semantic cache initialization complete enabled=%s",
                sem_cache.enabled,
            )
        # Dynamic model catalog refresh (provider-native catalogs plus
        # Codex/Copilot/OpenRouter/OpenCode/Xiaomi and models.dev). Initial
        # refresh is synchronous so
        # the pool has data on the first request; the background loop keeps it fresh.
        catalog_enabled = os.environ.get("TUSKER_CATALOG_ENABLED", "1").strip().lower()
        if catalog_enabled not in {"0", "false", "no", "off"}:
            try:
                from tusker_gateway.catalog import (
                    CatalogRegistry,
                    catalog_refresh_loop,
                )
                interval_secs = float(os.environ.get("TUSKER_CATALOG_REFRESH_SECS", "300"))
                registry = CatalogRegistry.default(config.get("providers"))
                # Wire API keys into catalog clients that need them.
                _wire_catalog_api_keys(
                    registry, config.get("provider_api_keys", {}),
                    codex_rotator=app.get("codex_rotator"),
                    credential_rotators=app.get("credential_rotators"),
                    provider_registry=config.get("providers"),
                )
                # Wire into PoolManager so extend_pools_with_catalog() can read.
                pool_manager = app.get("pool_manager")
                app["catalog_registry"] = registry
                if pool_manager is not None:
                    pool_manager.catalog_registry = registry
                startup_timeout_secs = float(
                    os.environ.get("TUSKER_CATALOG_STARTUP_TIMEOUT_SECS", "10")
                )
                catalog_started = asyncio.get_running_loop().time()
                try:
                    await asyncio.wait_for(
                        registry.refresh_all(app["http_session"]),
                        timeout=startup_timeout_secs,
                    )
                except asyncio.TimeoutError:
                    startup_log.warning(
                        "catalog initial refresh timed out after %.1fs; continuing with static pools",
                        startup_timeout_secs,
                    )
                except Exception as exc:
                    startup_log.warning(
                        "catalog initial refresh failed; continuing with static pools: %s",
                        exc,
                    )
                startup_log.info(
                    "catalog initial refresh window elapsed=%.1fs",
                    asyncio.get_running_loop().time() - catalog_started,
                )
                if pool_manager is not None:
                    catalog_counts = pool_manager.extend_pools_with_catalog()
                    auto_free = pool_manager.extend_pools_with_free_catalog()
                    startup_log.info(
                        "catalog confirmed %d pool entries; auto_free pools: %s",
                        sum(catalog_counts.values()),
                        {k: len(v) for k, v in auto_free.items()},
                    )
                    startup_log.info(
                        "catalog inventory providers=%s",
                        {
                            client.provider: len(client._entries)
                            for client in registry._clients.values()
                        },
                    )
                app["catalog_task"] = asyncio.create_task(
                    catalog_refresh_loop(
                        registry,
                        app["http_session"],
                        interval_secs,
                        stop_event,
                        on_refresh=(
                            pool_manager.extend_pools_with_free_catalog
                            if pool_manager is not None
                            else None
                        ),
                    ),
                    name="catalog-refresh",
                )
                startup_log.info(
                    "catalog refresh task started (interval=%.0fs, providers=%s)",
                    interval_secs,
                    ", ".join(sorted({c.provider for c in registry._clients.values()})),
                )
            except Exception as exc:
                startup_log.warning("catalog refresh failed to start: %s", exc)
        capabilities_enabled = os.environ.get(
            "TUSKER_CAPABILITIES_ENABLED", "1"
        ).strip().lower()
        # RTK (token-saver) opt-in. Default OFF — RTK is a best-effort
        # text compressor and we want operators to enable it explicitly
        # once they've reviewed the filter set. See tusker_gateway.rtk.
        rtk_enabled = os.environ.get(
            "TUSKER_RTK_ENABLED", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        from tusker_gateway.rtk import set_enabled as rtk_set_enabled

        # Set the global explicitly in both directions. This matters for
        # in-process reloads/tests where a previous app instance may have
        # enabled RTK before a new instance starts with the flag off.
        rtk_set_enabled(rtk_enabled)
        app["rtk_enabled"] = rtk_enabled
        startup_log.info(
            "rtk token-saver state enabled=%s env=TUSKER_RTK_ENABLED",
            rtk_enabled,
        )
        from tusker_gateway.tool_formats import tool_diagnostics_enabled

        startup_log.info(
            "tool markup diagnostics state enabled=%s env=TUSKER_TOOL_DIAGNOSTICS",
            tool_diagnostics_enabled(),
        )
        startup_log.info(
            "provider auth inventory static_keys=%s credential_pools=%s",
            sorted(
                provider for provider, key in config.get("provider_api_keys", {}).items()
                if key
            ),
            {
                provider: len(credentials)
                for provider, credentials in config.get("credential_pools", {}).items()
            },
        )
        if capabilities_enabled not in {"0", "false", "no", "off"}:
            interval_secs = float(
                os.environ.get("TUSKER_CAPABILITIES_REFRESH_SECS", "3600")
            )
            app["capabilities_task"] = asyncio.create_task(
                capabilities_refresh_loop(
                    app["capability_registry"],
                    app["http_session"],
                    interval_secs,
                    stop_event,
                ),
                name="capabilities-refresh",
            )
            # The loop performs its first refresh immediately. Yield once so
            # startup requests see discovered media providers when available.
            await asyncio.sleep(0)
            startup_log.info(
                "capability refresh task started (interval=%.0fs)",
                interval_secs,
            )

    async def on_cleanup(app):
        # Stop refresh tasks before closing the shared HTTP session.
        stop_event = app.get("refresh_stop_event")
        if stop_event is not None:
            stop_event.set()
        for task_name in ("catalog_task", "capabilities_task"):
            task = app.get(task_name)
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        tracer = app.get("tracer")
        if tracer is not None and getattr(tracer, "enabled", False):
            await tracer.stop()
        sem_cache = app.get("semantic_cache")
        if sem_cache is not None and sem_cache.enabled:
            await sem_cache.close()
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
    app.router.add_post("/v1/images/generations", images_handler)
    app.router.add_post("/v1/images/edits", images_handler)
    app.router.add_post("/v1/images/variations", images_handler)
    app.router.add_post("/v1/audio/speech", tts_handler)
    app.router.add_post("/v1/videos", video_handler)
    app.router.add_get("/v1/models", models_handler)
    app.router.add_post("/v1/chat/completions", chat_completions_handler)
    app.router.add_post("/v1/responses", responses_handler)
    app.router.add_post("/v1/messages", anthropic_messages_handler)

    app.on_cleanup.append(on_cleanup)
    app.on_startup.append(on_startup)
    return app


def _wire_catalog_api_keys(
    registry,
    provider_keys: dict[str, str | None],
    codex_rotator: "CodexTokenRotator | None" = None,
    credential_rotators: dict[str, "CodexTokenRotator"] | None = None,
    provider_registry: dict[str, Any] | None = None,
) -> None:
    """Inject configured bearer keys into authenticated catalog clients.

    Static bearer providers (openrouter/opencode/xiaomi) get a long-lived
    API key via ``set_api_key``. Codex and Copilot need per-refresh
    OAuth tokens, so they get an async ``set_token_source`` closure that
    resolves the current rotator token (or the raw GitHub token, for
    Copilot) on each fetch — matching the chat-path auth strategy.
    """
    startup_log = logging.getLogger("tusker_gateway.startup")
    provider_keys = {
        str(provider).lower(): value
        for provider, value in (provider_keys or {}).items()
    }
    provider_registry = provider_registry or {}
    # Wire OAuth token sources for Codex and Copilot providers
    # (must come before API key injection to ensure proper order)
    
    def _rotator_for(provider: str):
        if credential_rotators is None:
            return codex_rotator
        return credential_rotators.get(provider)

    async def _codex_token_source() -> str | None:
        rotator = _rotator_for("openai-codex")
        if rotator is None:
            return None
        return await rotator.get_token()

    codex_client = registry.get_client("openai-codex")
    if codex_client is not None:
        codex_client.set_token_source(_codex_token_source)

    # Public vs enterprise Copilot use different raw tokens. Fetch the
    # public token from provider_api_keys["github-copilot"] and the
    # enterprise token from provider_api_keys["github-copilot-enterprise"]
    # so each catalog client authenticates against its own host.
    copilot_raw_key = provider_keys.get("github-copilot")
    copilot_ent_raw_key = provider_keys.get("github-copilot-enterprise")
    copilot_rotator = _rotator_for("github-copilot")
    copilot_ent_rotator = _rotator_for("github-copilot-enterprise")

    async def _copilot_token_source() -> str | None:
        # Mirrors OAuthAuthenticator: a static GitHub token (gho_/
        # github_pat_) wins over the OAuth rotator so operators can
        # override per-deployment, and falls back to the rotator.
        if copilot_raw_key:
            return copilot_raw_key
        if copilot_rotator is None:
            return None
        return await copilot_rotator.get_token()

    async def _copilot_ent_token_source() -> str | None:
        if copilot_ent_raw_key:
            return copilot_ent_raw_key
        if copilot_ent_rotator is None:
            return None
        return await copilot_ent_rotator.get_token()

    copilot_client = registry.get_client("github-copilot")
    if copilot_client is not None:
        copilot_client.set_token_source(_copilot_token_source)
    copilot_ent_client = registry.get_client("github-copilot-enterprise")
    if copilot_ent_client is not None:
        copilot_ent_client.set_token_source(_copilot_ent_token_source)

    # Inject API keys for all configured providers to their catalog clients.
    for provider in provider_keys:
        client = registry.get_client(provider)
        if client is not None:
            client.set_api_key(provider_keys.get(provider))

    # Report only providers whose configured auth mode actually requires a
    # credential. models.dev and local catalogs are intentionally public or
    # local and should not create misleading 401/403 warnings.
    for provider in registry._clients:
        client = registry.get_client(provider)
        if client is None:
            continue

        if provider == "models.dev":
            continue
        config = provider_registry.get(provider)
        if isinstance(config, dict):
            kind = config.get("kind", config.get("auth_type"))
        else:
            kind = getattr(config, "kind", None)
            if kind is None:
                kind = getattr(config, "auth_type", None)
        if str(kind or "").lower() in {"local", "none", "public"}:
            continue

        has_api_key = bool(provider_keys.get(provider))
        has_token_source = bool(getattr(client, "_token_source", None))
        if not has_api_key and not has_token_source:
            startup_log.warning(
                "provider %s catalog has no authentication configured; "
                "refresh may return 401/403",
                provider,
            )

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
