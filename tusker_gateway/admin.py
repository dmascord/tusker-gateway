"""Authenticated admin API (read + write) for Tusker Gateway operators.

All endpoints require a valid API key (``Authorization: Bearer <key>``) or a
session cookie.  The caller's identity is re-resolved on every request so
key revocation and scope changes take effect immediately.

Write operations (``POST``, ``PUT``, ``DELETE``) additionally require:
  - ``admin:write`` scope (in addition to ``admin:read``)
  - For cookie-session callers: a valid CSRF token via
    ``X-CSRF-Token`` request header matching the value embedded in the
    session cookie (``/admin/session`` returns the current token).
  - ``Origin`` header must match the request host or configured proxy
    host; cross-site requests are rejected with 403.

Routes
-------
GET  /admin/diagnostics   — aggregated view of every subsystem
GET  /admin/providers    — configured providers, base URLs, auth kind, key status
GET  /admin/pools        — pool model lists, validity counts, auto-catalog sources
GET  /admin/catalog      — per-provider catalog refresh state
GET  /admin/cooldowns    — active per-model/provider cooldowns
GET  /admin/breakers    — circuit breaker states
GET  /admin/keys         — API key fingerprints, principals, and identity metadata
GET  /admin/usage        — per-provider usage counters and capacity state
GET  /admin/config       — full editable config snapshot (credentials redacted)
POST /admin/keys         — create a new managed identity API key
PUT  /admin/keys/{fp}    — update an identity profile
DELETE /admin/keys/{fp}  — revoke/delete an identity
POST /admin/keys/{fp}/rotate — rotate an identity's raw key
PUT  /admin/providers/{provider}       — create or replace a provider definition
DELETE /admin/providers/{provider}     — remove a provider
PUT  /admin/providers/{provider}/settings  — update provider runtime settings
PUT  /admin/providers/{provider}/credentials — update provider API key/credentials
PUT  /admin/pools/{pool}         — create or replace a pool definition
DELETE /admin/pools/{pool}       — remove a pool

Response format
---------------
All endpoints return JSON. Errors use the OpenAI error shape::

    {"error": {"message": "...", "code": "...", "type": "..."}}
"""
from __future__ import annotations

import functools
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import secrets
import time
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from tusker_gateway.config_store import ConfigUnavailableError
from tusker_gateway.errors import AuthorizationError, BadRequestError, GatewayError, NotFoundError
from tusker_gateway.identity import CallerIdentity, fingerprint_api_key
from tusker_gateway.storage import storage_status


# ─── Security helpers ────────────────────────────────────────────────────────


def _validate_csrf(record: dict[str, Any], request: web.Request) -> None:
    """Raise AuthorizationError if the CSRF header mismatches the session."""
    token = request.headers.get("X-CSRF-Token", "")
    expected = str(record.get("csrf_token") or "")
    if not token or not expected or not secrets.compare_digest(token, expected):
        raise AuthorizationError("Missing or invalid CSRF token", code="invalid_csrf")


def _validate_origin(request: web.Request) -> None:
    """Require and validate Origin header for cookie-session writes."""
    origin = request.headers.get("Origin")
    if not origin:
        raise AuthorizationError("Origin header required", code="missing_origin")
    try:
        parsed = urlsplit(origin)
        origin_host = parsed.netloc.rsplit("@", 1)[-1].lower()
    except ValueError:
        raise AuthorizationError("Invalid Origin header", code="invalid_origin")
    if not origin_host:
        raise AuthorizationError("Invalid Origin header", code="invalid_origin")
    request_host = (request.host or "").lower()
    if origin_host == request_host:
        return
    proxy = os.environ.get("TUSKER_PROXY_HOST", "").strip().lower()
    if proxy and origin_host == proxy:
        return
    raise AuthorizationError("Origin host not allowed", code="origin_mismatch")


def _require_write_scope(identity: CallerIdentity) -> None:
    """Ensure an explicit identity allows both admin:read and admin:write."""
    if identity is None or not getattr(identity, "managed", False):
        raise AuthorizationError("Explicit managed admin identity required", code="insufficient_scope")
    if not identity.allows_scope("admin:read"):
        raise AuthorizationError("Identity does not allow admin:read", code="insufficient_scope")
    if not identity.allows_scope("admin:write"):
        raise AuthorizationError("Identity does not allow admin:write", code="insufficient_scope")


logger = logging.getLogger(__name__)


# ─── Session management ────────────────────────────────────────────────────────

_SESSION_COOKIE = "tusker_admin_session"
_SESSION_TTL_SECS = 8 * 3600
_LOGIN_RATE_LIMIT = 5          # attempts
_LOGIN_RATE_WINDOW_SECS = 60.0


class SessionManager:
    """HMAC-signed, in-memory admin sessions.

    A session cookie value is ``<sid>.<expiry>.<hmac(sid.expiry)>``.  The
    signing key is derived from the configured API keys, so rotating the
    key set invalidates every outstanding session (fail-closed).  Issued
    session IDs live in a process-local set: a gateway restart logs all
    admin browsers out, which is the desired failure mode.
    """

    def __init__(self, signing_keys: list[str], ttl_secs: int = _SESSION_TTL_SECS):
        self._keys = [k for k in signing_keys if k]
        self._ttl = ttl_secs
        self._issued: dict[str, dict[str, Any]] = {}   # sid -> session record

    @property
    def cookie_name(self) -> str:
        return _SESSION_COOKIE

    def _sign(self, message: str) -> str:
        # Any configured key can verify: derive from the concatenation so a
        # single HMAC key survives key-set changes mid-session pool.
        material = "|".join(sorted(self._keys)).encode("utf-8")
        return hmac_mod.new(material, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue(
        self,
        principal: str = "",
        tenant: str = "",
        *,
        fingerprint: str = "",
        scopes: tuple[str, ...] = (),
    ) -> tuple[str, float]:
        """Return ``(cookie_value, expiry_epoch)`` for a fresh session.

        The session record captures the issuing key fingerprint and the
        scopes at issuance so the middleware can re-resolve the identity
        on every request (revocation / scope removal take effect
        immediately) and so write routes can require ``admin:write``.
        """
        sid = secrets.token_hex(16)
        expiry = time.time() + self._ttl
        self._issued[sid] = {
            "expiry": expiry,
            "principal": principal,
            "tenant": tenant,
            "fingerprint": fingerprint,
            "scopes": list(scopes),
            "csrf_token": secrets.token_urlsafe(32),
        }
        self._gc()
        return f"{sid}.{int(expiry)}.{self._sign(f'{sid}.{int(expiry)}')}", expiry

    def validate(self, cookie_value: str | None) -> dict[str, Any] | None:
        """Return the session record when the cookie is valid, else None."""
        if not cookie_value:
            return None
        parts = cookie_value.split(".")
        if len(parts) != 3:
            return None
        sid, expiry_raw, signature = parts
        if not sid or not expiry_raw.isdigit():
            return None
        expected = self._sign(f"{sid}.{expiry_raw}")
        if not hmac_mod.compare_digest(signature, expected):
            return None
        expiry = int(expiry_raw)
        record = self._issued.get(sid)
        if record is None:
            # Signed but never issued by this process (restart or forged).
            return None
        if time.time() > expiry:
            self._issued.pop(sid, None)
            return None
        return record

    def revoke(self, sid: str) -> None:
        self._issued.pop(sid, None)

    def _gc(self) -> None:
        now = time.time()
        expired = [sid for sid, rec in self._issued.items() if now > rec["expiry"]]
        for sid in expired:
            self._issued.pop(sid, None)


def get_session_manager(app: web.Application) -> SessionManager:
    """Return the process-wide session manager, creating it on first use."""
    manager = app.get("admin_sessions")
    if manager is None:
        config = app.get("config", {})
        manager = SessionManager([str(k) for k in config.get("api_keys", [])])
        app["admin_sessions"] = manager
    return manager


class _LoginRateLimiter:
    """Fixed-window per-IP limiter for login attempts."""

    def __init__(self, max_attempts: int = _LOGIN_RATE_LIMIT, window: float = _LOGIN_RATE_WINDOW_SECS):
        self._max = max_attempts
        self._window = window
        self._attempts: dict[str, list[float]] = {}

    def allow(self, identity: str) -> bool:
        now = time.time()
        recent = [t for t in self._attempts.get(identity, []) if now - t < self._window]
        if len(recent) >= self._max:
            self._attempts[identity] = recent
            return False
        recent.append(now)
        self._attempts[identity] = recent
        return True

    def reset(self) -> None:
        self._attempts.clear()


_login_limiter = _LoginRateLimiter()

# ─── Route handlers ────────────────────────────────────────────────────────────



async def admin_diagnostics(request: web.Request) -> web.Response:
    """GET /admin/diagnostics — aggregated view of every subsystem.

    Returns the same fields as ``GET /status`` plus the per-provider catalog
    refresh state and the circuit-breaker snapshot.
    """
    from tusker_gateway.config import load_config
    from tusker_gateway.cooldown import global_tracker
    from tusker_gateway.circuit_breaker import CircuitBreaker
    from tusker_gateway.rate_limit import RateLimiter
    from tusker_gateway.provider_usage import (
        ProviderUsageDB,
        capacity_controller,
        default_provider_usage_db_path,
    )
    from tusker_gateway.quality import QualityDB
    from tusker_gateway.pools import PoolManager

    config = load_config()
    pools = request.app.get("pool_manager") or PoolManager(config)
    quality = QualityDB(config["quality_db_path"])
    provider_usage = ProviderUsageDB(default_provider_usage_db_path(config["quality_db_path"]))
    breaker: CircuitBreaker | None = request.app.get("breaker")
    ratelimit: RateLimiter | None = request.app.get("ratelimit")
    catalog_reg = request.app.get("catalog_registry")

    try:
        from tusker_gateway.persistent_cooldown import PersistentCooldownStore
        from pathlib import Path
        db_path = Path(config.get("quality_db_path", "data/quality.db")).parent / "cooldowns.db"
        store = PersistentCooldownStore(db_path=db_path)
        store.purge_expired()
    except Exception:
        pass  # non-fatal; global_tracker snapshot will reflect in-memory state

    result: dict[str, Any] = {
        "pools": pools.status(),
        "catalog": catalog_reg.diagnostics() if catalog_reg else {},
        "quality": quality.status(),
        "provider_usage": provider_usage.status(),
        "provider_capacity": capacity_controller().snapshot(),
        "cooldowns": global_tracker().snapshot(),
        "circuit_breakers": breaker.snapshot() if breaker else {},
        "rate_limiter_stats": ratelimit.stats_snapshot() if ratelimit else {},
        "state_store": storage_status(),
    }

    identity_store = request.app.get("identity_store")
    if identity_store is not None:
        identity_cfg = getattr(identity_store, "config", None)
        result["enterprise_controls"] = {
            "identity_profiles": len(getattr(identity_cfg, "identities", {})),
            "identity_required": bool(getattr(identity_cfg, "required", False)),
        }

    return web.json_response(result)


async def admin_providers(request: web.Request) -> web.Response:
    """GET /admin/providers — configured providers with base URLs and auth status.

    Raw API keys are never returned.  Each provider shows whether a key is
    configured and, for bearer-key providers, a SHA-256 fingerprint of the
    configured key so operators can correlate without exposing the secret.
    """
    config = request.app.get("config", {})
    provider_registry: dict[str, Any] = config.get("providers", {})
    provider_keys: dict[str, str | None] = config.get("provider_api_keys", {})
    excluded: set[str] = {p.lower() for p in config.get("excluded_providers", [])}
    disabled: set[str] = {p.lower() for p in config.get("disabled_providers", [])}

    result: dict[str, Any] = {}
    for name, prov in provider_registry.items():
        name_lower = str(name).lower()
        raw_key = provider_keys.get(name_lower)
        result[str(name)] = {
            "name": str(name),
            "base_url": getattr(prov, "base_url", None),
            "chat_path": getattr(prov, "chat_path", None),
            "auth_kind": getattr(prov, "auth_type", None) or getattr(prov, "kind", "bearer"),
            "has_key": bool(raw_key),
            "key_fingerprint": (
                hashlib.sha256(raw_key.encode()).hexdigest()[:16]
                if raw_key else None
            ),
            "models_path": getattr(prov, "models_path", None),
            "rerank_path": getattr(prov, "rerank_path", None),
            "model_header": getattr(prov, "model_header", None),
            "zdr_ok": getattr(prov, "zdr_ok", False),
            "heavyweight": getattr(prov, "heavyweight", False),
            "excluded": name_lower in excluded,
            "disabled": name_lower in disabled,
        }

    return web.json_response({"providers": result})


async def admin_pools(request: web.Request) -> web.Response:
    """GET /admin/pools — current pool definitions and model lists."""
    from tusker_gateway.pools import PoolManager
    from tusker_gateway.config import load_config

    pools = request.app.get("pool_manager") or PoolManager(load_config())
    return web.json_response({"pools": pools.status()})


async def admin_catalog(request: web.Request) -> web.Response:
    """GET /admin/catalog — per-provider catalog refresh state and entry counts."""
    catalog_reg = request.app.get("catalog_registry")
    if catalog_reg is None:
        return web.json_response({"catalog": {}})

    diag = catalog_reg.diagnostics()
    # Strip error messages from disabled/error providers — those can contain
    # sensitive upstream details and are not useful for an admin overview.
    for provider, d in diag.items():
        if d.get("last_refresh_status") != "ok":
            d.pop("last_error", None)
            d.pop("last_error_class", None)

    return web.json_response({"catalog": diag})


async def admin_cooldowns(request: web.Request) -> web.Response:
    """GET /admin/cooldowns — active per-model/provider cooldowns."""
    from tusker_gateway.cooldown import global_tracker

    return web.json_response({"cooldowns": global_tracker().snapshot()})


async def admin_breakers(request: web.Request) -> web.Response:
    """GET /admin/breakers — circuit breaker states."""
    breaker = request.app.get("breaker")
    if breaker is None:
        return web.json_response({"breakers": {}})
    return web.json_response({"breakers": breaker.snapshot()})


async def admin_keys(request: web.Request) -> web.Response:
    """GET /admin/keys — API key fingerprints, principals, and identity metadata.

    Raw keys are never returned.  Each entry shows the SHA-256 fingerprint
    (stable, non-secret identifier), the resolved principal, tenant, and any
    allowlists configured for that key.
    """
    identity_store = request.app.get("identity_store")
    if identity_store is None:
        return web.json_response({"keys": []})

    identities: dict[str, Any] = {}
    for fp, identity in identity_store.config.identities.items():
        identities[fp] = {
            "fingerprint": fp,
            "principal": identity.principal,
            "tenant": identity.tenant,
            "scopes": list(identity.scopes),
            "allowed_pools": list(identity.allowed_pools),
            "allowed_models": list(identity.allowed_models),
            "allowed_providers": list(identity.allowed_providers),
            "managed": identity.managed,
        }

    return web.json_response({
        "keys": list(identities.values()),
        "total": len(identities),
        "strict_identity": identity_store.config.required,
    })


async def admin_usage(request: web.Request) -> web.Response:
    """GET /admin/usage — per-provider usage counters and capacity state."""
    from tusker_gateway.config import load_config
    from tusker_gateway.provider_usage import (
        ProviderUsageDB,
        capacity_controller,
        default_provider_usage_db_path,
    )
    from tusker_gateway.rate_limit import RateLimiter

    config = load_config()
    provider_usage = ProviderUsageDB(default_provider_usage_db_path(config["quality_db_path"]))
    ratelimit: RateLimiter | None = request.app.get("ratelimit")

    result: dict[str, Any] = {
        "provider_usage": provider_usage.status(),
        "provider_capacity": capacity_controller().snapshot(),
    }
    if ratelimit is not None:
        result["rate_limiter_stats"] = ratelimit.stats_snapshot()
        result["rate_limiter_buckets"] = ratelimit.snapshot()

    return web.json_response(result)


def _admin_identity_has_access(identity: Any) -> bool:
    """Return whether a resolved identity may use the admin console.

    Admin access requires an explicit identity profile with the
    ``admin:read`` scope.  Legacy unscoped keys (no profile) are
    intentionally rejected so the admin console has a dedicated key
    rather than inheriting every chat key in the pool.
    """
    if identity is None or not identity.managed:
        return False
    return identity.allows_scope("admin:read")


async def admin_login(request: web.Request) -> web.Response:
    """POST /admin/login — exchange an API key for a session cookie.

    Accepts JSON ``{"api_key": "..."}`` or a form field ``api_key`` so the
    HTML console works without JavaScript.  The key must be one of the
    configured ``API_KEYS`` and its identity must allow the ``admin:read``
    scope (legacy unscoped keys qualify).  Sets an HttpOnly, SameSite=Lax,
    Secure session cookie scoped to ``/admin``.
    """
    peer = request.remote or "unknown"
    if not _login_limiter.allow(peer):
        return web.json_response(
            openai_error_shape("too many login attempts; retry shortly", "rate_limited"),
            status=429,
        )
    api_key = ""
    if request.content_type == "application/json" or request.content_type.endswith("+json"):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            api_key = str(body.get("api_key") or "")
    else:
        data = await request.post()
        api_key = str(data.get("api_key") or "")
    api_key = api_key.strip()

    allowed = request.app.get("config", {}).get("api_keys", [])
    if not any(secrets.compare_digest(api_key, str(candidate)) for candidate in allowed):
        logger.warning("admin login failed: invalid key")
        return web.json_response(
            openai_error_shape("invalid API key", "invalid_api_key"),
            status=401,
        )

    identity = None
    identity_store = request.app.get("identity_store")
    if identity_store is not None:
        try:
            identity = identity_store.resolve(api_key)
        except GatewayError:
            return web.json_response(
                openai_error_shape("key has no admin identity", "identity_profile_required"),
                status=403,
            )

    if not _admin_identity_has_access(identity):
        return web.json_response(
            openai_error_shape("key is not authorized for admin access", "insufficient_scope"),
            status=403,
        )

    manager = get_session_manager(request.app)
    cookie_value, expiry = manager.issue(
        principal=identity.principal,
        tenant=identity.tenant,
        fingerprint=identity.key_fingerprint,
        scopes=tuple(identity.scopes),
    )
    resp = web.json_response({
        "ok": True,
        "principal": identity.principal,
        "tenant": identity.tenant,
        "expires_at": int(expiry),
    })
    resp.set_cookie(
        manager.cookie_name,
        cookie_value,
        max_age=int(expiry - time.time()),
        path="/admin",
        httponly=True,
        samesite="Lax",
        secure=request.secure or request.headers.get("X-Forwarded-Proto") == "https",
    )
    return resp


async def admin_logout(request: web.Request) -> web.Response:
    """POST /admin/logout — revoke the session cookie."""
    manager = get_session_manager(request.app)
    record = manager.validate(request.cookies.get(manager.cookie_name))
    if record is not None:
        sid = request.cookies[manager.cookie_name].split(".", 1)[0]
        manager.revoke(sid)
    resp = web.json_response({"ok": True})
    resp.del_cookie(manager.cookie_name, path="/admin")
    return resp

async def admin_session(request: web.Request) -> web.Response:
    """GET /admin/session — report the caller's login state for the SPA.

    Session callers receive their CSRF token here; every write request from
    the console must echo it back in the ``X-CSRF-Token`` header.
    """
    record = request.get("admin_session_record")
    identity = request.get("identity")
    if isinstance(record, dict):
        # Session-cookie caller: expose the CSRF token and the identity's
        # CURRENT scopes (re-resolved by the middleware, not the stale
        # scopes captured at issuance).
        scopes = list(identity.scopes) if isinstance(identity, CallerIdentity) else list(record.get("scopes") or [])
        return web.json_response({
            "authenticated": True,
            "principal": identity.principal if isinstance(identity, CallerIdentity) else (record.get("principal") or "admin"),
            "tenant": identity.tenant if isinstance(identity, CallerIdentity) else (record.get("tenant") or "default"),
            "scopes": scopes,
            "via": "session",
            "csrf_token": record.get("csrf_token") or "",
            "can_write": identity.allows_scope("admin:write") if isinstance(identity, CallerIdentity) else ("admin:write" in scopes),
        })
    if isinstance(identity, CallerIdentity):
        return web.json_response({
            "authenticated": True,
            "principal": identity.principal,
            "tenant": identity.tenant,
            "scopes": list(identity.scopes),
            "via": "api_key",
            "can_write": identity.allows_scope("admin:write"),
        })
    return web.json_response({"authenticated": False})


def openai_error_shape(message: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "code": code, "type": "invalid_request_error"}}


def _store_unavailable_shape() -> dict[str, Any]:
    """Sanitized 503 body for any config-store failure (no exception detail)."""
    return openai_error_shape("configuration store unavailable", "store_unavailable")


async def admin_page(request: web.Request) -> web.Response:
    """GET /admin/ — serve the admin console SPA (login gate is client-side;
    every data call is server-side session-guarded regardless)."""
    return web.Response(
        status=200,
        text=ADMIN_CONSOLE_HTML,
        content_type="text/html",
        charset="utf-8",
    )


ADMIN_CONSOLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Tusker Gateway — Admin</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root { --bg:#0e1116; --card:#161b22; --border:#30363d; --fg:#e6edf3; --muted:#8b949e; --ok:#3fb950; --warn:#d29922; --err:#f85149; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; }
    header { padding:12px 24px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; gap:12px; }
    header h1 { font-size:17px; margin:0; }
    main { padding:20px; display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:14px; }
    .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:14px; overflow:auto; max-height:520px; }
    .card h2 { font-size:12px; margin:0 0 10px; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em; }
    table { width:100%; border-collapse:collapse; font-size:12px; }
    th, td { padding:5px 8px; text-align:left; border-bottom:1px solid var(--border); white-space:nowrap; }
    th { color:var(--muted); font-weight:500; position:sticky; top:0; background:var(--card); }
    td.wrap { white-space:normal; word-break:break-all; }
    .ok { color:var(--ok); } .warn { color:var(--warn); } .err { color:var(--err); }
    .meta { color:var(--muted); font-size:12px; }
    .badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; }
    .badge.ok { background:rgba(63,185,80,0.15); color:var(--ok); }
    .badge.err { background:rgba(248,81,73,0.15); color:var(--err); }
    button { background:#21262d; color:var(--fg); border:1px solid var(--border); border-radius:6px; padding:6px 14px; cursor:pointer; font-size:13px; }
    button:hover { background:#30363d; }
    input[type=password] { background:#0d1117; color:var(--fg); border:1px solid var(--border); border-radius:6px; padding:8px 10px; font-size:14px; width:100%; }
    .login-box { max-width:420px; margin:12vh auto; padding:28px; background:var(--card); border:1px solid var(--border); border-radius:10px; }
    .login-box h1 { font-size:18px; margin:0 0 6px; }
    .login-box p { color:var(--muted); margin:0 0 18px; font-size:13px; }
    .login-box input { margin-bottom:12px; }
    .login-box button { width:100%; padding:9px; }
    .error { color:var(--err); font-size:13px; margin:8px 0; min-height:16px; }
    .wide { grid-column: 1 / -1; }
    pre { margin:0; font-size:11px; color:var(--muted); overflow:auto; }
  </style>
</head>
<body>
  <div id="login" class="login-box" style="display:none">
    <h1>Tusker Gateway Admin</h1>
    <p>Sign in with an admin-capable API key.</p>
    <form id="login-form">
      <input type="password" id="api-key" placeholder="API key" autocomplete="off" required>
      <div class="error" id="login-error"></div>
      <button type="submit">Sign in</button>
    </form>
  </div>
  <div id="console" style="display:none">
    <header>
      <h1>Tusker Gateway Admin</h1>
      <span class="meta" id="who"></span>
      <button id="logout">Sign out</button>
    </header>
    <main>
      <section class="card"><h2>Providers</h2><div id="providers">loading…</div></section>
      <section class="card"><h2>Pools</h2><div id="pools">loading…</div></section>
      <section class="card"><h2>Catalog</h2><div id="catalog">loading…</div></section>
      <section class="card"><h2>Circuit Breakers</h2><div id="breakers">loading…</div></section>
      <section class="card"><h2>Cooldowns</h2><div id="cooldowns">loading…</div></section>
      <section class="card"><h2>API Keys (identities)</h2><div id="keys">loading…</div></section>
      <section class="card"><h2>Config (Providers / Pools / Settings)</h2><div id="config-matrix">loading…</div></section>
      <section class="card"><h2>Key Management</h2><div id="keys-admin">loading…</div></section>
      <section class="card wide"><h2>Usage &amp; Capacity</h2><div id="usage">loading…</div></section>
      <section class="card wide"><h2>State Store</h2><div id="diagnostics">loading…</div></section>
    </main>
  </div>
  <script>
    const $ = (id) => document.getElementById(id);
    const esc = (s) => { const d = document.createElement('div'); d.textContent = String(s ?? ''); return d.innerHTML; };

    async function api(path) {
      const resp = await fetch(path, { credentials: 'same-origin' });
      if (resp.status === 401) { showLogin(); throw new Error('unauthenticated'); }
      if (!resp.ok) throw new Error((await resp.json().catch(() => ({})))?.error?.message || resp.statusText);
      return resp.json();
    }

    function table(headers, rows) {
      if (!rows || !rows.length) return '<span class="meta">empty</span>';
      const head = headers.map((h) => `<th>${esc(h)}</th>`).join('');
      const body = rows.map((r) => `<tr>${r.map((c) => `<td class="wrap">${c}</td>`).join('')}</tr>`).join('');
      return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function showLogin() { $('login').style.display = ''; $('console').style.display = 'none'; }
    function showConsole() { $('login').style.display = 'none'; $('console').style.display = ''; }

    $('login-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      $('login-error').textContent = '';
      const resp = await fetch('/admin/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: $('api-key').value }),
      });
      if (resp.ok) { $('api-key').value = ''; await boot(); }
      else {
        const err = (await resp.json().catch(() => ({})))?.error?.message || 'login failed';
        $('login-error').textContent = err;
      }
    });

    $('logout').addEventListener('click', async () => {
      await fetch('/admin/logout', { method: 'POST', credentials: 'same-origin' });
      showLogin();
    });

    let csrfToken = '';
    let canWrite = false;

    async function boot() {
      try {
        const sess = await api('/admin/session');
        $('who').textContent = `${sess.principal} @ ${sess.tenant} — scopes: ${(sess.scopes || []).join(', ') || 'none'}`;
        csrfToken = sess.csrf_token || '';
        canWrite = !!sess.can_write;
        showConsole();
      } catch { showLogin(); return; }
      loadAll();
    }

    async function loadAll() {
      try {
        const [providers, pools, catalog, breakers, cooldowns, keys, usage, diag] = await Promise.all([
          api('/admin/providers'), api('/admin/pools'), api('/admin/catalog'),
          api('/admin/breakers'), api('/admin/cooldowns'), api('/admin/keys'),
          api('/admin/usage'), api('/admin/diagnostics'),
        ]);

        $('providers').innerHTML = table(
          ['name', 'auth', 'key', 'base URL', 'state'],
          Object.values(providers.providers).map((p) => [
            `<b>${esc(p.name)}</b>`, esc(p.auth_kind),
            p.has_key ? '<span class="ok">configured</span>' : '<span class="warn">none</span>',
            esc(p.base_url || ''),
            p.disabled ? '<span class="err">disabled</span>' : p.excluded ? '<span class="warn">excluded</span>' : '<span class="ok">active</span>',
          ])
        );

        const poolRows = Object.entries(pools.pools || {}).map(([name, p]) => [
          `<b>${esc(name)}</b>`, p.models, p.valid_candidates, p.invalid_candidates,
          (p.auto_catalog_providers || []).map(esc).join(', ') || '—',
        ]);
        $('pools').innerHTML = table(['pool', 'models', 'valid', 'invalid', 'auto-catalog'], poolRows)
          + `<details><summary class="meta">model lists</summary><pre>${esc(JSON.stringify(
              Object.fromEntries(Object.entries(pools.pools || {}).map(([n, p]) => [n, (p.candidates || []).map((c) => `${c.provider}/${c.model}`)])), null, 1))}</pre></details>`;

        $('catalog').innerHTML = table(
          ['provider', 'auth', 'entries', 'refresh', 'stale'],
          Object.entries(catalog.catalog || {}).map(([name, c]) => [
            esc(name), esc(c.auth_source || '—'), c.entries,
            esc(c.last_refresh_status || '—'),
            c.stale ? '<span class="warn">stale</span>' : '<span class="ok">fresh</span>',
          ])
        );

        const brk = Object.values(breakers.breakers || {});
        $('breakers').innerHTML = table(
          ['route', 'state', 'failures', 'opened'],
          brk.map((b) => [
            `${esc(b.provider)}/${esc(b.model)}`,
            b.state === 'closed' ? '<span class="ok">closed</span>' : `<span class="err">${esc(b.state)}</span>`,
            b.window_failures, b.opened_at ? new Date(b.opened_at * 1000).toLocaleTimeString() : '—',
          ])
        );

        const cd = cooldowns.cooldowns || {};
        const cdRows = [].concat(
          (cd.models || []).map((m) => [`${esc(m.provider)}/${esc(m.model)}`, 'model', m.reason || '', m.remaining_secs ?? '']),
          (cd.providers || []).map((p) => [esc(p.provider), 'provider', p.reason || '', p.remaining_secs ?? ''])
        );
        $('cooldowns').innerHTML = table(['route', 'kind', 'reason', 'remaining (s)'], cdRows);

        $('keys').innerHTML = keys.total
          ? table(['fingerprint', 'principal', 'tenant', 'scopes', 'pools'],
              keys.keys.map((k) => [ `<code>${esc(k.fingerprint.slice(0, 12))}…</code>`, esc(k.principal), esc(k.tenant),
                (k.scopes || []).map(esc).join(','), (k.allowed_pools || []).map(esc).join(',')]))
          : '<span class="meta">no identity profiles configured (legacy keys)</span>';

        const u = usage.provider_usage || {};
        $('usage').innerHTML = table(
          ['provider', 'requests', 'input tokens', 'output tokens', 'errors'],
          Object.entries(u.providers || u || {}).filter(([, v]) => typeof v === 'object').map(([name, v]) => [
            esc(name), v.requests ?? v.count ?? '—', v.input_tokens ?? '—', v.output_tokens ?? '—', v.errors ?? '—',
          ])
        ) + `<pre>${esc(JSON.stringify(usage.provider_capacity || {}, null, 1))}</pre>`;

        const ds = diag.state_store || {};
        $('diagnostics').innerHTML = table(
          ['backend', 'configured', 'degraded', 'last error'],
          [[esc(ds.backend || '—'), String(ds.configured), ds.degraded ? '<span class="err">degraded</span>' : '<span class="ok">healthy</span>', esc(ds.last_error || 'none')]]
        ) + `<pre>${esc(JSON.stringify({pools: Object.fromEntries(Object.entries(diag.pools || {}).map(([n, p]) => [n, {models: p.models, valid: p.valid_candidates}]))}, null, 1))}</pre>`;
      } catch (err) {
        if (String(err) !== 'Error: unauthenticated') console.error(err);
      }
      await loadConfigMatrix();
      await loadKeysAdmin();
    }

    // ─── write-side UI ───
    async function writeJSON(method, path, body) {
      const resp = await fetch(path, {
        method,
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
        body: JSON.stringify(body),
      });
      if (resp.status === 401) { showLogin(); throw new Error('unauthenticated'); }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error?.message || resp.statusText);
      return data;
    }

    function textArea(id, value, rows) {
      return `<textarea id="${id}" rows="${rows}" style="width:100%;font-family:monospace;font-size:11px;background:#0d1117;color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:8px">${esc(value)}</textarea>`;
    }

    async function loadConfigMatrix() {
      const box = $('config-matrix');
      if (!canWrite) {
        box.innerHTML = '<span class="meta">read-only session — admin:write scope required for config changes</span>';
        $('keys-admin').innerHTML = box.innerHTML;
        return;
      }
      let cfg;
      try { cfg = await api('/admin/config'); }
      catch (e) { box.innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }

      // Provider matrix
      const prov = cfg.providers || {};
      const pools = cfg.pools || {};
      const settings = cfg.settings || {};
      let html = '<h3 class="meta">Provider definitions</h3>';
      for (const name of Object.keys(prov)) {
        html += `<details><summary><b>${esc(name)}</b></summary>
          ${textArea('prov-json-' + esc(name), JSON.stringify(prov[name], null, 2), 8)}
          <div style="display:flex;gap:8px;margin:6px 0">
            <button data-act="prov-save" data-name="${esc(name)}">Save</button>
            <button data-act="prov-del" data-name="${esc(name)}">Delete</button>
          </div>
          <div class="error" id="prov-err-${esc(name)}"></div>
          <h4 class="meta">Runtime settings</h4>
          ${textArea('prov-set-' + esc(name), JSON.stringify(settings[name] || {}, null, 2), 4)}
          <div style="margin:6px 0"><button data-act="prov-set-save" data-name="${esc(name)}">Save settings</button></div>
          <h4 class="meta">Credentials (write-only — never displayed)</h4>
          <input type="password" id="prov-cred-${esc(name)}" placeholder="api_key (leave blank to keep)" style="width:70%">
          <div style="margin:6px 0"><button data-act="prov-cred-save" data-name="${esc(name)}">Save credentials</button>
          <div class="error" id="prov-cred-err-${esc(name)}"></div></div>
        </details>`;
      }
      html += `<details><summary><b>+ New provider</b></summary>
        <input id="new-prov-name" placeholder="provider name" style="width:40%">
        ${textArea('new-prov-json', '{\n  "base_url": "",\n  "chat_path": "/chat/completions"\n}', 5)}
        <div style="margin:6px 0"><button data-act="prov-new">Create</button><div class="error" id="new-prov-err"></div></div>
      </details>`;

      html += '<h3 class="meta">Pool definitions</h3>';
      for (const name of Object.keys(pools)) {
        html += `<details><summary><b>${esc(name)}</b></summary>
          ${textArea('pool-json-' + esc(name), JSON.stringify(pools[name], null, 2), 8)}
          <div style="display:flex;gap:8px;margin:6px 0">
            <button data-act="pool-save" data-name="${esc(name)}">Save</button>
            <button data-act="pool-del" data-name="${esc(name)}">Delete</button>
          </div>
          <div class="error" id="pool-err-${esc(name)}"></div>
        </details>`;
      }
      html += `<details><summary><b>+ New pool</b></summary>
        <input id="new-pool-name" placeholder="pool name" style="width:40%">
        ${textArea('new-pool-json', '{\n  "models": []\n}', 4)}
        <div style="margin:6px 0"><button data-act="pool-new">Create</button><div class="error" id="new-pool-err"></div></div>
      </details>`;
      box.innerHTML = html;

      box.addEventListener('click', async (ev) => {
        const act = ev.target?.dataset?.act;
        if (!act) return;
        const name = ev.target.dataset.name || '';
        const errBox = (id, msg) => { const e = $(id); if (e) e.textContent = msg || ''; };
        try {
          if (act === 'prov-save') {
            const body = JSON.parse($('prov-json-' + name).value);
            await writeJSON('PUT', `/admin/providers/${encodeURIComponent(name)}`, body);
            errBox('prov-err-' + name, 'saved'); await loadAll();
          } else if (act === 'prov-del') {
            await writeJSON('DELETE', `/admin/providers/${encodeURIComponent(name)}`);
            await loadAll();
          } else if (act === 'prov-set-save') {
            const body = JSON.parse($('prov-set-' + name).value);
            await writeJSON('PUT', `/admin/providers/${encodeURIComponent(name)}/settings`, body);
            errBox('prov-err-' + name, 'saved');
          } else if (act === 'prov-cred-save') {
            const v = $('prov-cred-' + name).value.trim();
            const body = v ? { api_key: v } : {};
            await writeJSON('PUT', `/admin/providers/${encodeURIComponent(name)}/credentials`, body);
            $('prov-cred-' + name).value = '';
            errBox('prov-cred-err-' + name, 'saved');
          } else if (act === 'prov-new') {
            const n = $('new-prov-name').value.trim();
            const body = JSON.parse($('new-prov-json').value);
            await writeJSON('PUT', `/admin/providers/${encodeURIComponent(n)}`, body);
            await loadAll();
          } else if (act === 'pool-save') {
            const body = JSON.parse($('pool-json-' + name).value);
            await writeJSON('PUT', `/admin/pools/${encodeURIComponent(name)}`, body);
            errBox('pool-err-' + name, 'saved'); await loadAll();
          } else if (act === 'pool-del') {
            await writeJSON('DELETE', `/admin/pools/${encodeURIComponent(name)}`);
            await loadAll();
          } else if (act === 'pool-new') {
            const n = $('new-pool-name').value.trim();
            const body = JSON.parse($('new-pool-json').value);
            await writeJSON('PUT', `/admin/pools/${encodeURIComponent(n)}`, body);
            await loadAll();
          }
        } catch (e) {
          const errId = act.startsWith('pool') ? (act === 'pool-new' ? 'new-pool-err' : 'pool-err-' + name)
            : (act === 'prov-new' ? 'new-prov-err' : act === 'prov-cred-save' ? 'prov-cred-err-' + name : 'prov-err-' + name);
          errBox(errId, e.message);
        }
      });
    }

    async function loadKeysAdmin() {
      const box = $('keys-admin');
      if (!canWrite) { box.innerHTML = '<span class="meta">read-only session</span>'; return; }
      let cfg;
      try { cfg = await api('/admin/config'); }
      catch (e) { box.innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
      const keys = cfg.keys || [];
      let html = '<h3 class="meta">Existing keys</h3>';
      for (const k of keys) {
        html += `<div style="margin:6px 0">
          <code>${esc(String(k.fingerprint || '').slice(0, 12))}…</code>
          ${esc(k.principal || '')} @ ${esc(k.tenant || '')} — ${(k.scopes || []).map(esc).join(', ')}
          ${k.revoked ? '<span class="err">revoked</span>' : ''}
          <button data-act="key-rot" data-fp="${esc(k.fingerprint)}">Rotate</button>
          <button data-act="key-del" data-fp="${esc(k.fingerprint)}">Revoke</button>
        </div>`;
      }
      html += `<h3 class="meta">Create new key</h3>
        ${textArea('new-key-json', JSON.stringify({ principal: "svc-new", tenant: "default", scopes: ["inference:chat"] }, null, 2), 6)}
        <div style="margin:6px 0"><button data-act="key-new">Create key</button><div class="error" id="new-key-err"></div></div>
        <div id="new-key-once" style="word-break:break-all"></div>`;
      box.innerHTML = html;

      box.addEventListener('click', async (ev) => {
        const act = ev.target?.dataset?.act;
        if (!act) return;
        const fp = ev.target.dataset.fp || '';
        const err = (id, m) => { const e = $(id); if (e) e.textContent = m || ''; };
        try {
          if (act === 'key-rot') {
            const out = await writeJSON('POST', `/admin/keys/${encodeURIComponent(fp)}/rotate`, {});
            $('new-key-once').innerHTML = `<span class="warn">New key (shown once): <code>${esc(out.api_key || '')}</code></span>`;
          } else if (act === 'key-del') {
            await writeJSON('DELETE', `/admin/keys/${encodeURIComponent(fp)}`);
            await loadAll();
          } else if (act === 'key-new') {
            const body = JSON.parse($('new-key-json').value);
            const out = await writeJSON('POST', '/admin/keys', body);
            $('new-key-once').innerHTML = `<span class="warn">API key (shown once): <code>${esc(out.api_key || '')}</code></span>`;
          }
        } catch (e) {
          err('new-key-err', e.message);
        }
      });
    }



    boot();
    setInterval(() => { if ($('console').style.display !== 'none') loadAll(); }, 15000);
  </script>
</body>
</html>"""




_OPEN_SESSION_PATHS = frozenset({"/admin/login", "/admin/logout"})


def attach_admin_access_middleware(app: web.Application) -> None:
    """Guard every /admin route with session-cookie or Bearer auth.

    ``/admin/login`` and ``/admin/logout`` are open.  Data routes accept
    either a valid admin session cookie (browser console) or the usual
    ``Authorization: Bearer <key>`` (scripts/curl).

    Session callers are re-authorized on EVERY request: the session record
    carries the issuing key fingerprint and the identity is re-resolved
    from the current identity store, so revoking a key or removing a
    scope immediately invalidates outstanding admin sessions.  Write
    methods from cookie sessions additionally require a valid CSRF token
    header and a same-origin (or configured proxy) ``Origin`` header.
    """
    _WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

    @web.middleware
    async def admin_access_middleware(request, handler):
        path = request.path
        if not (path == "/admin" or path.startswith("/admin/")):
            return await handler(request)
        # The console page itself is public static HTML; every data route
        # it calls is session/Bearer guarded server-side.
        if path in ("/admin", "/admin/") or path in _OPEN_SESSION_PATHS:
            return await handler(request)

        manager = get_session_manager(request.app)
        record = manager.validate(request.cookies.get(manager.cookie_name))
        if record is not None:
            request["admin_session_record"] = record
            # Re-resolve the identity for this request so revocation and
            # scope removal take effect without waiting for re-login.
            identity_store = request.app.get("identity_store")
            fingerprint = record.get("fingerprint") or ""
            current = None
            if identity_store is not None and fingerprint:
                current = identity_store.config.identities.get(fingerprint)
            if current is None or not _admin_identity_has_access(current):
                return web.json_response(
                    openai_error_shape(
                        "admin session is no longer authorized", "insufficient_scope"
                    ),
                    status=403,
                )
            request["identity"] = current
            if request.method in _WRITE_METHODS:
                try:
                    _validate_origin(request)
                    _validate_csrf(record, request)
                    _require_write_scope(current)
                except AuthorizationError as exc:
                    return web.json_response(
                        openai_error_shape(exc.message, exc.code), status=403
                    )
            return await handler(request)

        # Fall back to the standard Bearer path: resolve identity, then
        # enforce the admin scope here because the gateway authorization
        # middleware runs before identity is attached on /admin routes.
        from tusker_gateway.auth import AuthMiddleware
        auth = AuthMiddleware(request.app.get("identity_store"))
        try:
            await auth.verify(request)
        except GatewayError as exc:
            return web.json_response(
                openai_error_shape(exc.message, exc.code),
                status=exc.status,
            )
        identity = request.get("identity")
        if not _admin_identity_has_access(identity):
            return web.json_response(
                openai_error_shape(
                    "key is not authorized for admin access", "insufficient_scope"
                ),
                status=403,
            )
        if request.method in _WRITE_METHODS:
            try:
                _require_write_scope(identity)
            except AuthorizationError as exc:
                return web.json_response(
                    openai_error_shape(exc.message, exc.code), status=403
                )
        return await handler(request)

    app.middlewares.append(admin_access_middleware)


# ─── Config store write handlers ─────────────────────────────────────────────

_PROVIDER_DEF_FIELDS = frozenset({
    "name", "kind", "auth_type", "base_url", "chat_path",
    "auth_env", "pool_env", "model_header", "models_path",
    "rerank_path", "model_aliases", "zdr_ok", "heavyweight",
})
_PROVIDER_SETTINGS_FIELDS = frozenset({
    "enabled", "disabled", "passthrough_disabled",
    "authoritative_catalog", "heavyweight", "notes",
})
_POOL_FIELDS = frozenset({
    "name", "models", "context_window", "zdr",
    "provider_warmup_secs", "auto_free", "heavyweight_only",
    "auto_catalog_providers", "fallback_pools",
})
_IDENTITY_PROFILE_FIELDS = frozenset({
    "api_key", "name", "principal", "tenant", "scopes",
    "allowed_pools", "allowed_models", "allowed_providers",
    "revoked", "fingerprint",
})


def _get_config_store(request: web.Request) -> Any:
    store = request.app.get("config_store")
    if store is None:
        raise ConfigUnavailableError("configuration store unavailable")
    return store
async def _apply_reload(request: web.Request) -> None:
    """Trigger config-runtime reload so writes take effect without restart."""
    runtime = request.app.get("config_runtime")
    if runtime is None:
        return
    try:
        await runtime.apply_reload()
    except Exception:
        logger.debug("post-write apply_reload failed", exc_info=True)



def _reject_unknown_fields(body: dict, allowed: set[str], label: str) -> None:
    unknown = set(body) - allowed
    if unknown:
        raise BadRequestError(
            f"Unknown {label} fields: {', '.join(sorted(unknown))}",
            code="unknown_fields",
        )


async def _json_body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise BadRequestError("Invalid JSON body", code="malformed_payload")
    if not isinstance(data, dict):
        raise BadRequestError("JSON body must be an object", code="malformed_payload")
    return data


def _admin_error_guard(handler):
    """Convert escaped GatewayErrors on config-store handlers to JSON.

    ``/admin`` routes bypass the gateway auth middleware that converts
    ``GatewayError`` for chat routes, so the config-store handlers wrap
    themselves here (payload validation, missing resources, unexpected
    store failures all get a sanitized OpenAI-shaped response).
    """
    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except GatewayError as exc:
            return web.json_response(
                openai_error_shape(exc.message, exc.code or "invalid_request"),
                status=exc.status,
            )
        except ConfigUnavailableError:
            return web.json_response(_store_unavailable_shape(), status=503)
        except Exception:
            logger.exception("admin config handler failed")
            return web.json_response(_store_unavailable_shape(), status=503)

    return wrapper


# ─── Read config snapshot ────────────────────────────────────────────


@_admin_error_guard
async def admin_config(request: web.Request) -> web.Response:
    """GET /admin/config — full editable config snapshot; credentials are redacted."""
    try:
        store = _get_config_store(request)
        snapshot = store.snapshot()
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except Exception:
        logger.exception("config snapshot failed")
        return web.json_response(
            openai_error_shape("configuration unavailable", "store_unavailable"), status=503
        )
    if not isinstance(snapshot, dict):
        return web.json_response(
            openai_error_shape("invalid config snapshot", "store_error"), status=503
        )
    return web.json_response(snapshot)


# ─── Identity / client key CRUD ───────────────────────────────────────


@_admin_error_guard
async def admin_keys_create(request: web.Request) -> web.Response:
    """POST /admin/keys — create a new managed identity API key.

    Body is a JSON object with identity profile fields.  The store
    returns ``api_key`` only in this response; it is never stored
    or repeated by any other endpoint.
    """
    body = await _json_body(request)
    _reject_unknown_fields(body, _IDENTITY_PROFILE_FIELDS, "identity")
    try:
        store = _get_config_store(request)
        result = store.upsert_client_key(body)
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except KeyError as exc:
        return web.json_response(openai_error_shape(str(exc), "not_found"), status=404)
    except ValueError as exc:
        return web.json_response(openai_error_shape(str(exc), "malformed_payload"), status=400)
    except Exception:
        logger.exception("create client key failed")
        return web.json_response(
            openai_error_shape("configuration unavailable", "store_unavailable"), status=503
        )
    await _apply_reload(request)
    return web.json_response(result, status=201)


@_admin_error_guard
async def admin_keys_update(request: web.Request) -> web.Response:
    """PUT /admin/keys/{fingerprint} — update an identity profile (no raw key)."""
    fingerprint = request.match_info.get("fingerprint", "")
    if not fingerprint:
        return web.json_response(openai_error_shape("fingerprint required", "bad_request"), status=400)
    body = await _json_body(request)
    _reject_unknown_fields(body, _IDENTITY_PROFILE_FIELDS - {"api_key", "fingerprint"}, "identity")
    try:
        store = _get_config_store(request)
        result = store.upsert_client_key({**body, "fingerprint": fingerprint})
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except KeyError as exc:
        return web.json_response(openai_error_shape(str(exc), "not_found"), status=404)
    except ValueError as exc:
        return web.json_response(openai_error_shape(str(exc), "malformed_payload"), status=400)
    except Exception:
        logger.exception("update client key failed")
        return web.json_response(
            openai_error_shape("configuration unavailable", "store_unavailable"), status=503
        )
    await _apply_reload(request)
    return web.json_response(result)


@_admin_error_guard
async def admin_keys_delete(request: web.Request) -> web.Response:
    """DELETE /admin/keys/{fingerprint} — revoke and delete an identity."""
    fingerprint = request.match_info.get("fingerprint", "")
    if not fingerprint:
        return web.json_response(openai_error_shape("fingerprint required", "bad_request"), status=400)
    try:
        store = _get_config_store(request)
        store.revoke_client_key(fingerprint)
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except KeyError as exc:
        return web.json_response(openai_error_shape(str(exc), "not_found"), status=404)
    except ValueError as exc:
        return web.json_response(openai_error_shape(str(exc), "forbidden"), status=400)
    except Exception:
        logger.exception("revoke client key failed")
        return web.json_response(
            openai_error_shape("configuration unavailable", "store_unavailable"), status=503
        )
    await _apply_reload(request)
    return web.json_response({"ok": True, "fingerprint": fingerprint})


@_admin_error_guard
async def admin_keys_rotate(request: web.Request) -> web.Response:
    """POST /admin/keys/{fingerprint}/rotate — rotate an identity's raw key."""
    fingerprint = request.match_info.get("fingerprint", "")
    if not fingerprint:
        return web.json_response(openai_error_shape("fingerprint required", "bad_request"), status=400)
    try:
        store = _get_config_store(request)
        result = store.rotate_client_key(fingerprint)
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except KeyError as exc:
        return web.json_response(openai_error_shape(str(exc), "not_found"), status=404)
    except ValueError as exc:
        return web.json_response(openai_error_shape(str(exc), "malformed_payload"), status=400)
    except Exception:
        logger.exception("rotate client key failed")
        return web.json_response(
            openai_error_shape("configuration unavailable", "store_unavailable"), status=503
        )
    await _apply_reload(request)
    return web.json_response(result)


# ─── Provider CRUD ────────────────────────────────────────────────────


@_admin_error_guard
async def admin_providers_put(request: web.Request) -> web.Response:
    """PUT /admin/providers/{provider} — create or replace a provider definition."""
    provider = request.match_info.get("provider", "")
    if not provider:
        return web.json_response(openai_error_shape("provider name required", "bad_request"), status=400)
    body = await _json_body(request)
    if not isinstance(body, dict):
        return web.json_response(openai_error_shape("JSON body must be an object", "malformed_payload"), status=400)
    _reject_unknown_fields(body, _PROVIDER_DEF_FIELDS, "provider")
    try:
        store = _get_config_store(request)
        result = store.upsert_provider(body)
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except (KeyError, ValueError) as exc:
        code = "not_found" if isinstance(exc, KeyError) else "malformed_payload"
        return web.json_response(openai_error_shape(str(exc), code), status=400 if isinstance(exc, ValueError) else 404)
    except Exception:
        logger.exception("upsert provider failed")
        return web.json_response(openai_error_shape("configuration unavailable", "store_unavailable"), status=503)
    await _apply_reload(request)
    return web.json_response(result)


@_admin_error_guard
async def admin_providers_delete(request: web.Request) -> web.Response:
    """DELETE /admin/providers/{provider} — remove a provider definition."""
    provider = request.match_info.get("provider", "")
    if not provider:
        return web.json_response(openai_error_shape("provider name required", "bad_request"), status=400)
    try:
        store = _get_config_store(request)
        store.delete_provider(provider)
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except KeyError as exc:
        return web.json_response(openai_error_shape(str(exc), "not_found"), status=404)
    except ValueError as exc:
        return web.json_response(openai_error_shape(str(exc), "forbidden"), status=400)
    except Exception:
        logger.exception("delete provider failed")
        return web.json_response(openai_error_shape("configuration unavailable", "store_unavailable"), status=503)
    await _apply_reload(request)
    return web.json_response({"ok": True, "provider": provider})


@_admin_error_guard
async def admin_providers_settings_put(request: web.Request) -> web.Response:
    """PUT /admin/providers/{provider}/settings — update provider runtime toggles."""
    provider = request.match_info.get("provider", "")
    if not provider:
        return web.json_response(openai_error_shape("provider name required", "bad_request"), status=400)
    body = await _json_body(request)
    if not isinstance(body, dict):
        return web.json_response(openai_error_shape("JSON body must be an object", "malformed_payload"), status=400)
    _reject_unknown_fields(body, _PROVIDER_SETTINGS_FIELDS, "settings")
    try:
        store = _get_config_store(request)
        result = store.upsert_provider_settings(provider, body)
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except (KeyError, ValueError) as exc:
        code = "not_found" if isinstance(exc, KeyError) else "malformed_payload"
        return web.json_response(openai_error_shape(str(exc), code), status=400 if isinstance(exc, ValueError) else 404)
    except Exception:
        logger.exception("provider settings update failed")
        return web.json_response(openai_error_shape("configuration unavailable", "store_unavailable"), status=503)
    await _apply_reload(request)
    return web.json_response(result)


@_admin_error_guard
async def admin_providers_credentials_put(request: web.Request) -> web.Response:
    """PUT /admin/providers/{provider}/credentials — update provider API key/credentials."""
    provider = request.match_info.get("provider", "")
    if not provider:
        return web.json_response(openai_error_shape("provider name required", "bad_request"), status=400)
    body = await _json_body(request)
    if not isinstance(body, dict):
        return web.json_response(openai_error_shape("JSON body must be an object", "malformed_payload"), status=400)
    try:
        store = _get_config_store(request)
        result = store.upsert_provider_credentials(provider, body)
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except (KeyError, ValueError) as exc:
        code = "not_found" if isinstance(exc, KeyError) else "malformed_payload"
        return web.json_response(openai_error_shape(str(exc), code), status=400 if isinstance(exc, ValueError) else 404)
    except Exception:
        logger.exception("provider credentials update failed")
        return web.json_response(openai_error_shape("configuration unavailable", "store_unavailable"), status=503)
    await _apply_reload(request)
    return web.json_response(result)


# ─── Pool CRUD ────────────────────────────────────────────────────────


@_admin_error_guard
async def admin_pools_put(request: web.Request) -> web.Response:
    """PUT /admin/pools/{pool} — create or replace a pool definition."""
    pool = request.match_info.get("pool", "")
    if not pool:
        return web.json_response(openai_error_shape("pool name required", "bad_request"), status=400)
    body = await _json_body(request)
    if not isinstance(body, dict):
        return web.json_response(openai_error_shape("JSON body must be an object", "malformed_payload"), status=400)
    _reject_unknown_fields(body, _POOL_FIELDS, "pool")
    try:
        store = _get_config_store(request)
        result = store.upsert_pool(body)
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except (KeyError, ValueError) as exc:
        code = "not_found" if isinstance(exc, KeyError) else "malformed_payload"
        return web.json_response(openai_error_shape(str(exc), code), status=400 if isinstance(exc, ValueError) else 404)
    except Exception:
        logger.exception("upsert pool failed")
        return web.json_response(openai_error_shape("configuration unavailable", "store_unavailable"), status=503)
    await _apply_reload(request)
    return web.json_response(result)


@_admin_error_guard
async def admin_pools_delete(request: web.Request) -> web.Response:
    """DELETE /admin/pools/{pool} — remove a pool definition."""
    pool = request.match_info.get("pool", "")
    if not pool:
        return web.json_response(openai_error_shape("pool name required", "bad_request"), status=400)
    try:
        store = _get_config_store(request)
        store.delete_pool(pool)
    except ConfigUnavailableError:
        return web.json_response(_store_unavailable_shape(), status=503)
    except KeyError as exc:
        return web.json_response(openai_error_shape(str(exc), "not_found"), status=404)
    except ValueError as exc:
        return web.json_response(openai_error_shape(str(exc), "forbidden"), status=400)
    except Exception:
        logger.exception("delete pool failed")
        return web.json_response(openai_error_shape("configuration unavailable", "store_unavailable"), status=503)
    await _apply_reload(request)
    return web.json_response({"ok": True, "pool": pool})


def register_admin_config_routes(app: web.Application) -> None:
    """Register the read/write admin config endpoints on ``app``.

    Call from your application factory after ``attach_admin_access_middleware``
    so every route is protected by session/Bearer auth.  The caller's
    identity is re-resolved on every request (see the middleware).
    """
    app.router.add_get("/admin/config", admin_config)
    app.router.add_post("/admin/keys", admin_keys_create)
    app.router.add_put("/admin/keys/{fingerprint}", admin_keys_update)
    app.router.add_delete("/admin/keys/{fingerprint}", admin_keys_delete)
    app.router.add_post("/admin/keys/{fingerprint}/rotate", admin_keys_rotate)
    app.router.add_put("/admin/providers/{provider}", admin_providers_put)
    app.router.add_delete("/admin/providers/{provider}", admin_providers_delete)
    app.router.add_put("/admin/providers/{provider}/settings", admin_providers_settings_put)
    app.router.add_put("/admin/providers/{provider}/credentials", admin_providers_credentials_put)
    app.router.add_put("/admin/pools/{pool}", admin_pools_put)
    app.router.add_delete("/admin/pools/{pool}", admin_pools_delete)


