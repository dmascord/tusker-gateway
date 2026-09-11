"""Runtime configuration reload driven by the database-backed ConfigStore.

Keeps an in-process snapshot of the DB config and applies it on
generation changes: pool manager, catalog registry, capabilities
registry, token rotators, and identity store.  Falls back to the
last-known-good snapshot (or env fallback) when the store is
unavailable.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from aiohttp import web

# The store module is imported lazily/guardedly: DB-backed config is opt-in,
# and legacy deployments must start unchanged even when the module (or its
# DB driver) is unavailable.
try:
    from tusker_gateway.config_store import ConfigStore, ConfigUnavailableError
except Exception:  # pragma: no cover - opt-out deployments
    ConfigStore = None  # type: ignore[assignment]

    class ConfigUnavailableError(Exception):  # type: ignore[no-redef]
        """Raised when the DB-backed config store cannot be reached."""

from tusker_gateway.identity import IdentityConfig, IdentityStore, load_identity_config_from_env

logger = logging.getLogger(__name__)

_TUSKER_CONFIG_ENABLED_VARS = frozenset({"1", "true", "yes", "on"})


def _env_enabled() -> bool:
    return os.environ.get("TUSKER_CONFIG_DATABASE_ENABLED", "0").strip().lower() in _TUSKER_CONFIG_ENABLED_VARS


class ConfigRuntime:
    """Watches ConfigStore generation and applies changes to app subsystems."""

    def __init__(self, app: web.Application) -> None:
        self._app = app
        self._enabled = _env_enabled()
        self._generation: int = 0
        self._error: str | None = None
        self._last_good_runtime: dict[str, Any] | None = None
        self._last_good_identity: IdentityConfig | None = None
        self._last_authoritative: bool = False
        self._initial_refresh_tokens: frozenset[str] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._last_media_providers: frozenset | None = None
    async def apply_reload(self) -> bool:
        """Reload the store now and apply any generation change. Admin hook."""
        if not self.reload_now():
            return False
        try:
            cfg = self.runtime_config(self._app.get("config", {}))
        except Exception:
            return False
        self._apply(self._generation)
        self._app["config_generation"] = self._generation
        self._app["config_runtime_status"] = self.status()
        return True

    # -- public query API -------------------------------------------------

    def enabled(self) -> bool:
        """Return whether database-backed config is enabled."""
        return self._enabled

    def runtime_config(self, fallback: dict[str, Any]) -> dict[str, Any]:
        """Return the live runtime config dict, or fallback on unavailable."""
        store = self._store()
        if store is None or not self._enabled:
            return fallback
        try:
            snap = store.runtime_config(fallback)
            self._generation = store.generation
            self._error = None
            self._last_good_runtime = snap
            # Track authoritative state so auth knows whether to block dev bypass.
            self._last_authoritative = bool(snap.get("config_db_keys_authoritative"))
            return snap
        except ConfigUnavailableError as exc:
            self._error = self._redact(str(exc))
            if self._last_good_runtime is not None:
                return self._last_good_runtime
            return fallback

    def identity_config(self, fallback: IdentityConfig) -> IdentityConfig:
        """Return the live identity config, or fallback on unavailable."""
        store = self._store()
        if store is None or not self._enabled:
            return fallback
        try:
            snap = store.identity_config(fallback)
            self._generation = store.generation
            self._error = None
            self._last_good_identity = snap
            return snap
        except ConfigUnavailableError as exc:
            self._error = self._redact(str(exc))
            if self._last_good_identity is not None:
                return self._last_good_identity
            return fallback

    def _rebuild_identity_store(self) -> None:
        """Refresh ``app[\"identity_store\"]`` from the live DB-backed
        ``IdentityConfig``.  The auth middleware resolves identity on every
        request so the replacement takes effect immediately.
        """
        store = self._store()
        if store is None:
            return
        fallback_cfg = (
            self._app.get("identity_store").config
            if self._app.get("identity_store") is not None
            else load_identity_config_from_env()
        )
        new_cfg = store.identity_config(fallback_cfg)
        new_store = IdentityStore(new_cfg)
        self._app["identity_store"] = new_store
        logger.debug(
            "identity store refreshed: %d identities",
            len(new_cfg.identities),
        )


    def reload_now(self) -> bool:
        """Trigger an immediate store reload. Returns True on success."""
        store = self._store()
        if store is None or not self._enabled:
            return False
        try:
            store.reload_now()
            self._generation = store.generation
            self._error = None
            return True
        except ConfigUnavailableError as exc:
            self._error = self._redact(str(exc))
            return False

    def status(self) -> dict[str, Any]:
        """Current runtime status summary."""
        pool_manager = self._app.get("pool_manager")
        catalog_registry = self._app.get("catalog_registry")
        return {
            "enabled": self._enabled,
            "generation": self._generation,
            "error": self._error,
            "pool_count": len(pool_manager.pools) if pool_manager is not None else 0,
            "catalog_providers": list(catalog_registry.providers()) if catalog_registry is not None else [],
        }

    # -- lifecycle --------------------------------------------------------

    async def start(self, stop_event: asyncio.Event, interval_secs: float | None = None) -> None:
        """Start the background poll loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = stop_event
        self._loop = asyncio.get_running_loop()
        interval = interval_secs if interval_secs is not None else float(os.environ.get("TUSKER_CONFIG_REFRESH_SECS", "300"))
        # The background poll detects the first generation change and applies
        # it on the next tick. An eager _apply here would break tests that
        # monkeypatch it (and fails on mock stores with minimal config).
        self._task = asyncio.create_task(self._poll(stop_event, interval), name="config-runtime-reload")
    async def stop(self) -> None:
        """Stop the poll loop and join the task."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None and self._task is not asyncio.current_task():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None
        self._enabled = False

    # -- internals --------------------------------------------------------

    def _store(self):
        store = self._app.get("config_store")
        if store is None or ConfigStore is None:
            return None
        return store if isinstance(store, ConfigStore) else None

    @staticmethod
    def _redact(message: str) -> str:
        return "ConfigUnavailableError"

    def _apply(self, generation: int) -> None:
        """Rebuild all subsystems for the new generation on the event loop.

        Ordering matters: rotators before capabilities (capability probing
        resolves the codex rotator), validate the new snapshot by building
        the pool manager BEFORE publishing it to ``app["config"]`` — a
        malformed snapshot raises and the previous published config stays
        live (last-good semantics).
        """
        store = self._store()
        if store is None:
            return
        old_cfg = self._app.get("config", {})
        new_cfg = store.runtime_config(old_cfg)
        # Expose the store's CAS persistence callback so PassthroughClient
        # (which reads request.app["config"]) can wire rotators to persist
        # refreshed credentials back to the DB without importing the store.
        new_cfg["_persist_credentials"] = getattr(store, "persist_credentials", None)
        # Validate: build the new pool manager off-loop visible state first.
        # A failure here aborts the apply before anything is published.
        from tusker_gateway.pools import PoolManager
        candidate_pm = PoolManager(new_cfg)
        # Commit: publish the validated snapshot.
        self._app["config"] = new_cfg
        self._app["config_generation"] = generation
        self._app["config_runtime_status"] = self.status()
        self._capture_initial_refresh_tokens()
        candidate_pm._quality = self._app.get("quality_db")
        old_pm = self._app.get("pool_manager")
        candidate_pm.catalog_registry = (
            old_pm.catalog_registry if old_pm is not None else None
        )
        self._app["pool_manager"] = candidate_pm
        self._rebuild_rotators()
        self._rebuild_catalog()
        self._rebuild_capabilities(generation)
        self._rebuild_identity_store()
        try:
            from tusker_gateway.rtk import set_enabled as rtk_set_enabled
            rtk_set_enabled(bool(self._app.get("rtk_enabled", False)))
        except Exception as exc:
            logger.debug("rtk reapply failed: %s", exc)

    def _capture_initial_refresh_tokens(self) -> None:
        if self._initial_refresh_tokens is not None:
            return
        tokens: set[str] = set()
        for provider in ("openai-codex", "github-copilot"):
            rot = self._app.get("credential_rotators", {}).get(provider)
            if rot is None:
                continue
            for cred in getattr(rot, "_creds", []):
                token = cred.get("refresh_token")
                if token:
                    tokens.add(str(token))
        self._initial_refresh_tokens = frozenset(tokens)

    def _rebuild_pool_manager(self) -> None:
        store = self._store()
        if store is None:
            return
        new_cfg = store.runtime_config(self._app.get("config", {}))
        pm = self._app.get("pool_manager")
        if pm is not None:
            # Swap in a fresh PoolManager built from the new config;
            # stickiness is lost by design (snapshot consistency).
            from tusker_gateway.pools import PoolManager
            new_pm = PoolManager(new_cfg)
            new_pm._quality = self._app.get("quality_db")
            new_pm.catalog_registry = pm.catalog_registry
            self._app["pool_manager"] = new_pm

    def _rebuild_catalog(self) -> None:
        store = self._store()
        if store is None:
            return
        new_cfg = store.runtime_config(self._app.get("config", {}))
        old_registry = self._app.get("catalog_registry")
        # Detect provider-name, endpoint-URL, or key changes — a URL or key
        # edit alone must also rebuild, not just add/remove of a provider.
        def _provider_fingerprints(cfg: dict[str, Any]) -> dict[str, tuple]:
            providers = cfg.get("providers", {}) or {}
            keys = cfg.get("provider_api_keys", {}) or {}
            return {
                str(name).lower().replace("_", "-"): (
                    str(p.get("base_url", "")) if isinstance(p, dict) else str(getattr(p, "base_url", "")),
                    str(p.get("models_path", p.get("catalog_path", ""))) if isinstance(p, dict) else str(getattr(p, "models_path", getattr(p, "catalog_path", ""))),
                    str(keys.get(name, "")),
                )
                for name, p in providers.items()
            }
        old_fp = (
            {p: _provider_fingerprints(self._app.get("_config_prev_snapshot", {})).get(p) for p in old_registry.providers()}
            if old_registry is not None else {}
        )
        new_fp = _provider_fingerprints(new_cfg)
        old_names = set(old_registry.providers()) if old_registry is not None else set()
        new_names = set(new_cfg.get("providers", {}).keys())
        if old_names == new_names and all(old_fp.get(p) == new_fp.get(p) for p in new_names) and old_registry is not None:
            self._app["_config_prev_snapshot"] = new_cfg
            return  # no change; skip cancel/restart
        self._app["_config_prev_snapshot"] = new_cfg
        session = self._app.get("http_session")
        if session is None:
            return
        from tusker_gateway.catalog import CatalogRegistry, catalog_refresh_loop
        new_registry = CatalogRegistry.default(
            new_cfg.get("providers"),
            model_capability_db=self._app.get("model_capabilities"),
        )
        self._wire_catalog_api_keys(new_registry, new_cfg)
        pm = self._app.get("pool_manager")
        self._app["catalog_registry"] = new_registry
        if pm is not None:
            pm.catalog_registry = new_registry
        stop_event = self._app.get("refresh_stop_event")
        if stop_event is not None:
            # Cancel the existing refresh loop and start a fresh one.
            old_task = self._app.get("catalog_task")
            if old_task is not None:
                old_task.cancel()
            self._app["catalog_task"] = asyncio.create_task(
                catalog_refresh_loop(new_registry, session, float(os.environ.get("TUSKER_CATALOG_REFRESH_SECS", "300")), stop_event),
                name="catalog-refresh",
            )
        if pm is not None:
            pm.extend_pools_with_catalog()

    def _rebuild_capabilities(self, generation: int) -> None:
        store = self._store()
        if store is None:
            return
        new_cfg = store.runtime_config(self._app.get("config", {}))
        old = self._app.get("capability_registry")
        if old is not None and dict(old.provider_keys) == dict(new_cfg.get("provider_api_keys", {})):
            return  # keys unchanged
        session = self._app.get("http_session")
        if session is None:
            return
        from tusker_gateway.providers.capabilities import capabilities_refresh_loop, CapabilitiesRegistry
        new_reg = CapabilitiesRegistry(
            provider_keys=new_cfg.get("provider_api_keys", {}),
            codex_rotator=self._app.get("codex_rotator"),
            model_capability_db=self._app.get("model_capabilities"),
        )
        self._app["capability_registry"] = new_reg
        stop_event = self._app.get("refresh_stop_event")
        if stop_event is not None:
            old_task = self._app.get("capabilities_task")
            if old_task is not None:
                old_task.cancel()
            interval = float(os.environ.get("TUSKER_CAPABILITIES_REFRESH_SECS", "3600"))
            self._app["capabilities_task"] = asyncio.create_task(
                capabilities_refresh_loop(new_reg, session, interval, stop_event),
                name="capabilities-refresh",
            )

    def _rebuild_rotators(self) -> None:
        """Reload credential rotators from the new DB config.

        - Existing rotators keep their rotation cursor; ``.reload()`` is
          called ONLY when the credential content actually changed.
        - Removed providers: drop their rotator entry.
        - New providers: create fresh ``CodexTokenRotator`` instances.
        - Media handlers: rebuild only when the provider *set* changes.
        """
        store = self._store()
        if store is None:
            return
        new_cfg = store.runtime_config(self._app.get("config", {}))
        configured = new_cfg.get("credential_pools", {}) or {}
        existing: dict[str, Any] = dict(self._app.get("credential_rotators", {}))
        new_providers = set(configured.keys())
        old_providers = set(existing.keys())
        auth_file = new_cfg.get("auth_file")
        persist_cb = getattr(store, "persist_credentials", None)
        creds_authoritative = bool(new_cfg.get("config_db_credentials_authoritative"))

        # Media handlers snapshot provider URLs/keys at construction; the
        # fingerprint check inside _rebuild_media_handlers decides whether
        # a rebuild is needed.
        self._rebuild_media_handlers(new_cfg)

        result: dict[str, Any] = {}
        for provider, creds in configured.items():
            clean = [c for c in creds if isinstance(c, dict)]
            rot = existing.get(provider)
            if rot is not None:
                current_auth = getattr(rot, "_auth_file", None)
                if provider == "openai-codex" and current_auth != auth_file:
                    rot = None  # auth_file changed — persistence target moved
                elif getattr(rot, "_creds", None) == clean:
                    result[provider] = rot  # unchanged — preserve cursor
                    continue
                else:
                    rot.reload(clean)
                    result[provider] = rot
                    continue
            if rot is None:
                from tusker_gateway.passthrough import CodexTokenRotator
                provider_auth = auth_file if provider == "openai-codex" else None
                rot = CodexTokenRotator(
                    clean,
                    auth_file=provider_auth,
                    provider=provider,
                    persist_credentials=persist_cb,
                    secrets_authoritative=creds_authoritative,
                )
                session = self._app.get("http_session")
                if session is not None:
                    rot._http = session
            result[provider] = rot

        self._app["credential_rotators"] = result
        if "openai-codex" in result:
            self._app["codex_rotator"] = result["openai-codex"]
    def _rebuild_media_handlers(self, config: dict[str, Any]) -> None:
        """Rebuild media handlers when their relevant config changed.

        Compares a fingerprint of provider names, base URLs and API keys —
        not just the provider name set — so URL/key edits rebuild too.
        """
        providers = config.get("providers", {}) or {}
        keys = config.get("provider_api_keys", {}) or {}
        fingerprint = frozenset(
            (
                str(name).lower(),
                str(p.get("base_url", "")) if isinstance(p, dict) else str(getattr(p, "base_url", "")),
                str(keys.get(name, "")),
            )
            for name, p in providers.items()
        )
        if fingerprint == self._last_media_providers:
            return
        from tusker_gateway.providers.image_generation import ImageGenerationHandler
        from tusker_gateway.providers.tts import TTSHandler
        from tusker_gateway.providers.video import VideoHandler
        reg = self._app.get("capability_registry")
        self._app["image_handler"] = ImageGenerationHandler(config, capability_registry=reg)
        self._app["tts_handler"] = TTSHandler(config, capability_registry=reg)
        self._app["video_handler"] = VideoHandler(config, capability_registry=reg)
        self._last_media_providers = fingerprint


    def _wire_catalog_api_keys(self, registry: Any, config: dict[str, Any]) -> None:
        """Wire API keys + OAuth token sources into catalog clients.

        Mirrors ``app._wire_catalog_api_keys`` so config reloads don't drop the
        OAuth token sources for Codex/Copilot (which would leave ``_api_key``
        and ``_token_source`` both None and cause subsequent catalog refreshes
        to return HTTP 401 with ``auth=none``).
        """
        from tusker_gateway.app import _wire_catalog_api_keys
        _wire_catalog_api_keys(
            registry,
            config.get("provider_api_keys", {}),
            codex_rotator=self._app.get("codex_rotator"),
            credential_rotators=self._app.get("credential_rotators"),
            provider_registry=config.get("providers"),
        )

    # -- poll loop --------------------------------------------------------

    async def _poll(self, stop_event: asyncio.Event, interval_secs: float) -> None:
        while not stop_event.is_set():
            try:
                store = self._store()
                if store is None or not self._enabled:
                    await asyncio.sleep(interval_secs)
                    continue
                old_gen = self._generation
                # DB IO off the event loop.
                await asyncio.to_thread(store.reload_now)
                new_gen = store.generation
                if new_gen != old_gen:
                    self._generation = new_gen
                    self._error = None
                    logger.info("config generation changed %s -> %s; applying", old_gen, new_gen)
                    self._apply(new_gen)
            except ConfigUnavailableError as exc:
                self._error = self._redact(str(exc))
                logger.debug("config poll unavailable: %s", self._error)
            except Exception as exc:
                logger.warning("config poll error: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_secs)
                break
            except asyncio.TimeoutError:
                continue
