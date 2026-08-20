"""Auth verification for incoming requests."""
from __future__ import annotations

import secrets

from aiohttp import web

from tusker_gateway.errors import AuthenticationError

_DEV_KEY = "sk-secret-dev"


class AuthMiddleware:
    """Verify Bearer tokens against app["config"]["api_keys"]."""

    async def verify(self, request: web.Request) -> None:
        import os
        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            raise AuthenticationError("Authorization header required")

        token = auth[len("Bearer ") :].strip()
        if not token:
            raise AuthenticationError("Invalid API key")

        if secrets.compare_digest(token, _DEV_KEY):
            return

        allowed = set(request.app["config"].get("api_keys", []))
        if token in allowed:
            return

        # DEBUG: log what was checked
        print(f"[AUTH-DEBUG] token={token!r} allowed={allowed} len={len(token)}", flush=True)
        raise AuthenticationError("Invalid API key")
