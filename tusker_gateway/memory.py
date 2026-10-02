"""Authenticated proxy for selected Hindsight shared-memory operations."""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode

from aiohttp import ClientError, web
from tusker_gateway.identity import CallerIdentity


_DEFAULT_BASE_URL = "http://hindsight.hindsight.svc.cluster.local:8888"
_ROUTES = {
    ("POST", "/v1/memory/retain"): ("POST", "/memories"),
    ("POST", "/v1/memory/recall"): ("POST", "/memories/recall"),
    ("GET", "/v1/memory/list"): ("GET", "/memories/list"),
    ("GET", "/v1/memory/profile"): ("GET", "/profile"),
    ("PUT", "/v1/memory/profile"): ("PUT", "/profile"),
    ("DELETE", "/v1/memory/memories"): ("DELETE", "/memories"),
    ("DELETE", "/v1/memory/bank"): ("DELETE", ""),
    ("POST", "/v1/memory/reflect"): ("POST", "/reflect"),
    ("POST", "/v1/memory/consolidate"): ("POST", "/consolidate"),
    ("GET", "/v1/memory/stats"): ("GET", "/stats"),
    ("POST", "/v1/memory/health"): ("POST", "/health/llm"),
}


def _base_url() -> str:
    return os.environ.get("TUSKER_MEMORY_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _bank_path(bank: str, suffix: str) -> str:
    from urllib.parse import quote

    return f"/v1/default/banks/{quote(bank, safe='')}{suffix}"


def _request_bank(request: web.Request) -> str:
    bank = request.query.get("bank", "").strip()
    if not bank:
        raise ValueError("memory bank is required")
    if len(bank) > 128 or any(ch in bank for ch in "/\\\x00"):
        raise ValueError("invalid memory bank")
    return bank


def _authorize_bank(request: web.Request, bank: str) -> None:
    identity = request.get("identity")
    if not isinstance(identity, CallerIdentity) or not identity.allows_memory_bank(bank):
        raise PermissionError("Caller is not authorized for this memory bank")

async def memory_handler(request: web.Request) -> web.Response:
    route = _ROUTES.get((request.method, request.path))
    if route is None:
        raise web.HTTPNotFound()
    try:
        bank = _request_bank(request)
        _authorize_bank(request, bank)
    except ValueError as exc:
        return web.json_response({"error": {"message": str(exc), "code": "invalid_memory_bank"}}, status=400)
    except PermissionError as exc:
        return web.json_response({"error": {"message": str(exc), "code": "memory_bank_not_allowed"}}, status=403)

    upstream_method, suffix = route
    url = f"{_base_url()}{_bank_path(bank, suffix)}"
    params = [(key, value) for key, value in request.query.items() if key != "bank"]
    if params:
        url += "?" + urlencode(params)
    body: Any = None
    if request.can_read_body:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            return web.json_response({"error": {"message": "Invalid JSON body", "code": "malformed_payload"}}, status=400)
    session = request.app.get("http_session")
    if session is None:
        return web.json_response({"error": {"message": "memory service unavailable", "code": "memory_unavailable"}}, status=503)
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with session.request(upstream_method, url, json=body, headers=headers) as response:
            payload = await response.read()
            return web.Response(
                body=payload,
                status=response.status,
                headers={"Content-Type": response.headers.get("Content-Type", "application/json")},
            )
    except (ClientError, TimeoutError, RuntimeError):
        return web.json_response({"error": {"message": "memory service unavailable", "code": "memory_unavailable"}}, status=503)


__all__ = ["memory_handler"]
