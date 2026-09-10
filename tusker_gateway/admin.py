"""Authenticated read-only admin API for Tusker Gateway operators.

All endpoints require a valid API key (``Authorization: Bearer <key>``).
The caller's identity is resolved through the existing identity store, so
legacy keys (no identity profile) are accepted — they get full admin access
as they would for ``/status``.  Operators should treat admin API output as
operational data; no raw credentials are ever returned.

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

Response format
---------------
All endpoints return JSON. Errors use the OpenAI error shape::

    {"error": {"message": "...", "code": "...", "type": "..."}}
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import logging
import os
import secrets
import time
from typing import Any

from aiohttp import web

from tusker_gateway.errors import GatewayError
from tusker_gateway.identity import CallerIdentity, fingerprint_api_key
from tusker_gateway.storage import storage_status

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

    def issue(self, principal: str = "", tenant: str = "") -> tuple[str, float]:
        """Return ``(cookie_value, expiry_epoch)`` for a fresh session."""
        sid = secrets.token_hex(16)
        expiry = time.time() + self._ttl
        self._issued[sid] = {
            "expiry": expiry,
            "principal": principal,
            "tenant": tenant,
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
    """Return whether a resolved identity may use the admin console."""
    if identity is None:
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
        principal=identity.principal, tenant=identity.tenant
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
    """GET /admin/session — report the caller's login state for the SPA."""
    identity = request.get("identity")
    if isinstance(identity, CallerIdentity):
        return web.json_response({
            "authenticated": True,
            "principal": identity.principal,
            "tenant": identity.tenant,
            "scopes": list(identity.scopes),
            "via": "api_key",
        })
    record = request.get("admin_session_record")
    if isinstance(record, dict):
        return web.json_response({
            "authenticated": True,
            "principal": record.get("principal") or "admin",
            "tenant": record.get("tenant") or "default",
            "scopes": ["admin:read"],
            "via": "session",
        })
    return web.json_response({"authenticated": False})


def openai_error_shape(message: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "code": code, "type": "invalid_request_error"}}


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

    async function boot() {
      try {
        const sess = await api('/admin/session');
        $('who').textContent = `${sess.principal} @ ${sess.tenant}`;
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
    ``Authorization: Bearer <key>`` (scripts/curl).  Bearer callers still
    pass through identity authorization (admin:read scope) downstream.
    """
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
        return await handler(request)

    app.middlewares.append(admin_access_middleware)


