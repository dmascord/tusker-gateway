"""Auth verification for incoming requests."""
from __future__ import annotations

import logging
import secrets

from aiohttp import web

from tusker_gateway.errors import AuthenticationError

logger = logging.getLogger(__name__)

_DEV_KEY = "sk-secret-dev"


class AuthMiddleware:
    """Verify Bearer tokens against app["config"]["api_keys"]."""

    async def verify(self, request: web.Request) -> None:
        auth = request.headers.get("Authorization")
        x_api_key = request.headers.get("x-api-key", "")

        # Anthropic clients use x-api-key header instead of Authorization.
        token = ""
        if auth and auth.startswith("Bearer "):
            token = auth[len("Bearer ") :].strip()
        elif x_api_key:
            token = x_api_key.strip()

        if not token:
            raise AuthenticationError("Authorization header required")

        allowed = set(request.app["config"].get("api_keys", []))
        # Dev key bypass only when no production keys are configured.
        # In production, api_keys contains real secrets and the dev key should NOT work.
        if not allowed and secrets.compare_digest(token, _DEV_KEY):
            logger.debug('auth OK (dev key)')
            return

        if token in allowed:
            logger.debug('auth OK')
            return

        logger.warning('auth failed: invalid API key')
        raise AuthenticationError("Invalid API key")
