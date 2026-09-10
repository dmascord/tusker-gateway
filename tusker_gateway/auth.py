"""Auth verification for incoming requests."""
from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from aiohttp import web

from tusker_gateway.errors import AuthenticationError
from tusker_gateway.identity import extract_api_key

if TYPE_CHECKING:
    from tusker_gateway.identity import IdentityStore

logger = logging.getLogger(__name__)

_DEV_KEY = "sk-secret-dev"


class AuthMiddleware:
    """Verify Bearer tokens.

    Auth config is resolved from ``request.app`` on every request so that
    DB-backed config changes take effect immediately — no restart required.
    """

    def __init__(self, identity_store: "IdentityStore | None" = None) -> None:
        self._identity_store = identity_store

    def resolve_config(self, request: web.Request) -> dict:
        """Return the effective auth config for this request.

        When a DB-backed config runtime is present, its ``runtime_config``
        already applies last-good retention on outage and tracks whether the
        DB key section was authoritative, so auth can never silently weaken
        to env-only credentials mid-outage.
        """
        runtime = request.app.get("config_runtime")
        store = request.app.get("config_store")
        if runtime is None and store is None:
            return request.app["config"]
        if runtime is not None:
            try:
                cfg = runtime.runtime_config(request.app["config"])
            except Exception:
                cfg = None
            if cfg is not None and cfg.get("config_db_keys_authoritative"):
                return cfg
            # DB key section not authoritative: fall back to app config.
            return request.app["config"]
        # Store wired without runtime (tests): use store directly with a
        # one-shot authoritative latch so an outage cannot weaken auth.
        try:
            cfg = store.runtime_config(request.app["config"])
        except Exception:
            if getattr(store, "_saw_authoritative", False):
                return {"api_keys": [], "config_db_keys_authoritative": True}
            return request.app["config"]
        if cfg.get("config_latch", False) or cfg.get("config_db_keys_authoritative"):
            store._saw_authoritative = True
        if cfg.get("config_db_keys_authoritative"):
            return cfg
        return request.app["config"]

    def resolve_identity_store(self, request: web.Request) -> "IdentityStore | None":
        """Return the identity store for this request, if one is wired."""
        return request.app.get("identity_store") or self._identity_store

    async def verify(self, request: web.Request) -> None:
        token = extract_api_key(request)

        if not token:
            raise AuthenticationError("Authorization header required")

        cfg = self.resolve_config(request)
        allowed = cfg.get("api_keys", [])
        db_keys_authoritative = cfg.get("config_db_keys_authoritative", False)
        # Dev key bypass: only when no DB-authoritative key section is
        # active (legacy env-only deployment). An authoritative empty
        # section means "no keys permitted" — dev key must NOT work.
        if not allowed and not db_keys_authoritative and secrets.compare_digest(token, _DEV_KEY):
            logger.debug("auth OK (dev key)")
            self._attach_identity(request, token)
            return

        for candidate in allowed:
            if secrets.compare_digest(token, str(candidate)):
                logger.debug("auth OK")
                self._attach_identity(request, token)
                return

        logger.warning("auth failed: invalid API key")
        raise AuthenticationError("Invalid API key")

    def _attach_identity(self, request: web.Request, token: str) -> None:
        identity_store = self.resolve_identity_store(request)
        if identity_store is None:
            return
        identity = identity_store.resolve(token)
        request["identity"] = identity
        request["_api_key_fingerprint"] = identity.key_fingerprint
