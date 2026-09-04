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
    """Verify Bearer tokens against app["config"]["api_keys"]."""

    def __init__(self, identity_store: "IdentityStore | None" = None) -> None:
        self._identity_store = identity_store

    async def verify(self, request: web.Request) -> None:
        token = extract_api_key(request)

        if not token:
            raise AuthenticationError("Authorization header required")

        allowed = request.app["config"].get("api_keys", [])
        # Dev key bypass only when no production keys are configured.
        # In production, api_keys contains real secrets and the dev key should NOT work.
        if not allowed and secrets.compare_digest(token, _DEV_KEY):
            logger.debug('auth OK (dev key)')
            self._attach_identity(request, token)
            return

        for candidate in allowed:
            if secrets.compare_digest(token, str(candidate)):
                logger.debug('auth OK')
                self._attach_identity(request, token)
                return

        logger.warning('auth failed: invalid API key')
        raise AuthenticationError("Invalid API key")

    def _attach_identity(self, request: web.Request, token: str) -> None:
        if self._identity_store is None:
            return
        identity = self._identity_store.resolve(token)
        request["identity"] = identity
        request["_api_key_fingerprint"] = identity.key_fingerprint
