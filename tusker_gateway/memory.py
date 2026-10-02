"""Authenticated proxy for selected Hindsight and gateway memory operations."""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote, urlencode

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
    ("GET", "/v1/memory/health"): ("GET", "/health"),
}

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_HINDSIGHT_BANK_PREFIX = "/v1/default/banks/"


def _base_url() -> str:
    return os.environ.get("TUSKER_MEMORY_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _bank_path(bank: str, suffix: str) -> str:
    return f"/v1/default/banks/{quote(bank, safe='')}{suffix}"


def _request_bank(request: web.Request) -> str:
    bank = request.query.get("bank", "").strip()
    if not bank:
        raise ValueError("memory bank is required")
    if len(bank) > 128 or any(ch in bank for ch in "/\\\x00"):
        raise ValueError("invalid memory bank")
    return bank


def _authorize_bank(request: web.Request, bank: str, *, write: bool) -> None:
    identity = request.get("identity")
    scope = "memory:write" if write else "memory:read"
    if not isinstance(identity, CallerIdentity) or not identity.allows_scope(scope):
        raise PermissionError("Caller is not authorized for this memory operation")
    if not identity.allows_memory_bank(bank):
        raise PermissionError("Caller is not authorized for this memory bank")


async def _read_body(request: web.Request) -> Any:
    if not request.can_read_body:
        return None
    try:
        return await request.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": {"message": "Invalid JSON body", "code": "malformed_payload"}}),
            content_type="application/json",
        ) from exc


async def _forward(request: web.Request, method: str, url: str, body: Any = None) -> web.Response:
    session = request.app.get("http_session")
    if session is None:
        return web.json_response({"error": {"message": "memory service unavailable", "code": "memory_unavailable"}}, status=503)
    params = [(key, value) for key, value in request.query.items() if key != "bank"]
    if params:
        url += "?" + urlencode(params)
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with session.request(method, url, json=body, headers=headers) as response:
            return web.Response(body=await response.read(), status=response.status,
                                headers={"Content-Type": response.headers.get("Content-Type", "application/json")})
    except (ClientError, TimeoutError, RuntimeError):
        return web.json_response({"error": {"message": "memory service unavailable", "code": "memory_unavailable"}}, status=503)


async def memory_handler(request: web.Request) -> web.Response:
    route = _ROUTES.get((request.method, request.path))
    if route is None:
        raise web.HTTPNotFound()
    try:
        bank = _request_bank(request)
        _authorize_bank(request, bank, write=request.method not in _READ_METHODS)
    except ValueError as exc:
        return web.json_response({"error": {"message": str(exc), "code": "invalid_memory_bank"}}, status=400)
    except PermissionError as exc:
        return web.json_response({"error": {"message": str(exc), "code": "memory_bank_not_allowed"}}, status=403)
    upstream_method, suffix = route
    return await _forward(request, upstream_method, f"{_base_url()}{_bank_path(bank, suffix)}", await _read_body(request))


async def hindsight_compat_handler(request: web.Request) -> web.Response:
    """Proxy the Hindsight API shape used by OMP/OpenCode."""
    bank = request.match_info.get("bank", "")
    tail = request.match_info.get("tail", "")
    suffix = f"/{tail}" if tail else ""
    if not bank or len(bank) > 128 or any(ch in bank for ch in "/\\\x00"):
        raise web.HTTPBadRequest(text="invalid memory bank")
    try:
        _authorize_bank(request, bank, write=request.method not in _READ_METHODS)
    except PermissionError as exc:
        return web.json_response({"error": {"message": str(exc), "code": "memory_bank_not_allowed"}}, status=403)
    return await _forward(request, request.method, f"{_base_url()}{_bank_path(bank, suffix)}", await _read_body(request))


async def hindsight_version_handler(request: web.Request) -> web.Response:
    return await _forward(request, "GET", f"{_base_url()}/version")


__all__ = ["hindsight_compat_handler", "hindsight_version_handler", "memory_handler"]
