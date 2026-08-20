"""Server-rendered status dashboard.

A minimal HTML dashboard that aggregates the existing JSON endpoints
(/status, /metrics, /health, /ready) into a single page. Uses HTMX for
auto-refresh so we don't need a JS framework.

Routes:
    GET /dashboard                  — full HTML page
    GET /dashboard/partials/pools    — HTMX partial: pool health table
    GET /dashboard/partials/breakers — HTMX partial: circuit breaker states
    GET /dashboard/partials/cooldowns — HTMX partial: rate-limit cooldowns

Auth: same as /metrics (TUSKER_METRICS_TOKEN when set).
"""
from __future__ import annotations

import html
import os
import time
from typing import Any

from aiohttp import web

from tusker_gateway.budget import BudgetTracker
from tusker_gateway.cache import ResponseCache
from tusker_gateway.circuit_breaker import CircuitBreaker, BreakerState
from tusker_gateway.cooldown import global_tracker
from tusker_gateway.pools import PoolManager
from tusker_gateway.quality import QualityDB
from tusker_gateway.rate_limit import RateLimiter


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Tusker Gateway — Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://unpkg.com/htmx.org@1.9.10"></script>
  <style>
    :root {{ --bg:#0e1116; --card:#161b22; --border:#30363d; --fg:#e6edf3; --muted:#8b949e; --ok:#3fb950; --warn:#d29922; --err:#f85149; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; }}
    header {{ padding:16px 24px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }}
    header h1 {{ font-size:18px; margin:0; }}
    main {{ padding:24px; display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:16px; }}
    .card {{ background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px; }}
    .card h2 {{ font-size:14px; margin:0 0 12px; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th, td {{ padding:6px 8px; text-align:left; border-bottom:1px solid var(--border); }}
    th {{ color:var(--muted); font-weight:500; }}
    .ok {{ color:var(--ok); }}
    .warn {{ color:var(--warn); }}
    .err {{ color:var(--err); }}
    code {{ background:#0d1117; padding:1px 6px; border-radius:3px; font-size:12px; }}
    .meta {{ color:var(--muted); font-size:12px; }}
    .badge {{ display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; }}
    .badge.ok {{ background:rgba(63,185,80,0.15); color:var(--ok); }}
    .badge.warn {{ background:rgba(210,153,34,0.15); color:var(--warn); }}
    .badge.err {{ background:rgba(248,81,73,0.15); color:var(--err); }}
    .grid-2 {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
    .num {{ font-size:24px; font-weight:600; }}
  </style>
</head>
<body>
  <header>
    <h1>Tusker Gateway Dashboard</h1>
    <span class="meta" hx-get="/dashboard/partials/meta" hx-trigger="every 5s" hx-swap="innerHTML">
      loading…
    </span>
  </header>
  <main>
    <section class="card">
      <h2>Pools</h2>
      <div hx-get="/dashboard/partials/pools" hx-trigger="every 5s" hx-swap="innerHTML">loading…</div>
    </section>
    <section class="card">
      <h2>Circuit Breakers</h2>
      <div hx-get="/dashboard/partials/breakers" hx-trigger="every 5s" hx-swap="innerHTML">loading…</div>
    </section>
    <section class="card">
      <h2>Cooldowns (rate-limit 429s)</h2>
      <div hx-get="/dashboard/partials/cooldowns" hx-trigger="every 5s" hx-swap="innerHTML">loading…</div>
    </section>
    <section class="card">
      <h2>Cache &amp; Budgets</h2>
      <div hx-get="/dashboard/partials/quota" hx-trigger="every 5s" hx-swap="innerHTML">loading…</div>
    </section>
    <section class="card" style="grid-column: 1 / -1;">
      <h2>Recent Quality Events</h2>
      <div hx-get="/dashboard/partials/quality" hx-trigger="every 10s" hx-swap="innerHTML">loading…</div>
    </section>
  </main>
</body>
</html>
"""


def _esc(s: Any) -> str:
    return html.escape(str(s))


def _state_badge(state: BreakerState) -> str:
    klass = {"closed": "ok", "open": "err", "half_open": "warn"}.get(state.value, "")
    return f'<span class="badge {klass}">{_esc(state.value)}</span>'


async def dashboard_handler(request: web.Request) -> web.Response:
    return web.Response(
        status=200,
        text=PAGE,
        content_type="text/html",
        charset="utf-8",
    )


async def dashboard_meta(request: web.Request) -> web.Response:
    metrics = request.app.get("metrics")
    cache: ResponseCache | None = request.app.get("cache")
    budget: BudgetTracker | None = request.app.get("budget")
    breaker: CircuitBreaker | None = request.app.get("breaker")
    ratelimit: RateLimiter | None = request.app.get("ratelimit")

    parts = [f'updated {_esc(time.strftime("%H:%M:%S"))}']
    if metrics is not None:
        parts.append(f"v0.1.0")
    if cache is not None:
        s = cache.stats_snapshot()
        parts.append(f"cache: {s['hits']}h/{s['misses']}m")
    if breaker is not None:
        s = breaker.stats_snapshot()
        parts.append(f"breaker trips: {s['trips']}")
    if ratelimit is not None:
        s = ratelimit.stats_snapshot()
        parts.append(f"rate: {s['allowed']} ok / {s['blocked']} blocked")

    return web.Response(
        status=200,
        text=" · ".join(parts),
        content_type="text/html",
        charset="utf-8",
    )


async def dashboard_pools(request: web.Request) -> web.Response:
    config = request.app["config"]
    pool_mgr = PoolManager(config)
    rows: list[str] = []
    for name in sorted(pool_mgr.pools.keys()):
        valid = sum(1 for s in pool_mgr.models.get(name, []) if s.provider in config.get("providers", {}))
        invalid = len(pool_mgr.models.get(name, [])) - valid
        rows.append(
            f"<tr><td><code>{_esc(name)}</code></td>"
            f"<td>{len(pool_mgr.models.get(name, []))}</td>"
            f"<td class='ok'>{valid}</td>"
            f"<td class='{('err' if invalid else 'muted')}'>{invalid}</td></tr>"
        )
    html_out = (
        "<table><thead><tr><th>name</th><th>candidates</th><th>valid</th><th>invalid</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )
    return web.Response(status=200, text=html_out, content_type="text/html", charset="utf-8")


async def dashboard_breakers(request: web.Request) -> web.Response:
    breaker: CircuitBreaker | None = request.app.get("breaker")
    if breaker is None or not breaker._config.enabled:  # noqa: SLF001
        return web.Response(
            status=200,
            text="<span class='meta'>circuit breaker disabled</span>",
            content_type="text/html",
            charset="utf-8",
        )
    snap = breaker.snapshot()
    if not snap:
        return web.Response(
            status=200,
            text="<span class='meta'>no breaker state yet</span>",
            content_type="text/html",
            charset="utf-8",
        )
    rows = []
    for key in sorted(snap.keys()):
        entry = snap[key]
        state = BreakerState(entry["state"])
        rows.append(
            f"<tr><td><code>{_esc(entry['provider'])}</code></td>"
            f"<td><code>{_esc(entry['model'])}</code></td>"
            f"<td>{_state_badge(state)}</td>"
            f"<td>{entry['consecutive_failures']}</td>"
            f"<td>{entry['window_failures']}/{entry['window_total']}</td></tr>"
        )
    return web.Response(
        status=200,
        text=(
            "<table><thead><tr><th>provider</th><th>model</th><th>state</th><th>consec</th><th>win fail/total</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>"
        ),
        content_type="text/html",
        charset="utf-8",
    )


async def dashboard_cooldowns(request: web.Request) -> web.Response:
    tracker = global_tracker()
    entries = []
    now = time.time()
    for (provider, model), cooldown in list(tracker._cooldowns.items()):  # noqa: SLF001
        if cooldown.until > now:
            entries.append((provider, model, cooldown.until - now))
    if not entries:
        return web.Response(
            status=200,
            text="<span class='meta'>no active cooldowns</span>",
            content_type="text/html",
            charset="utf-8",
        )
    rows = []
    for provider, model, remaining in sorted(entries, key=lambda e: -e[2]):
        rows.append(
            f"<tr><td><code>{_esc(provider)}</code></td>"
            f"<td><code>{_esc(model)}</code></td>"
            f"<td>{remaining:.0f}s</td></tr>"
        )
    return web.Response(
        status=200,
        text=(
            "<table><thead><tr><th>provider</th><th>model</th><th>remaining</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>"
        ),
        content_type="text/html",
        charset="utf-8",
    )


async def dashboard_quota(request: web.Request) -> web.Response:
    cache: ResponseCache | None = request.app.get("cache")
    budget: BudgetTracker | None = request.app.get("budget")
    ratelimit: RateLimiter | None = request.app.get("ratelimit")
    out = []
    if cache is not None:
        s = cache.stats_snapshot()
        out.append(
            f"<div><span class='meta'>cache</span><div class='num'>{s['hits']}/{s['misses']}</div>"
            f"<span class='meta'>hit/miss · {s['writes']} writes · {s['evictions']} evictions</span></div>"
        )
    if budget is not None:
        s = budget.stats_snapshot()
        out.append(
            f"<div><span class='meta'>budget blocks</span><div class='num'>{s['blocks_daily'] + s['blocks_monthly'] + s['blocks_pool']}</div>"
            f"<span class='meta'>daily {s['blocks_daily']} · monthly {s['blocks_monthly']} · pool {s['blocks_pool']}</span></div>"
        )
    if ratelimit is not None:
        s = ratelimit.stats_snapshot()
        out.append(
            f"<div><span class='meta'>rate limit</span><div class='num'>{s['allowed']}</div>"
            f"<span class='meta'>allowed · {s['blocked']} blocked</span></div>"
        )
    if not out:
        return web.Response(
            status=200,
            text="<span class='meta'>no quota modules enabled</span>",
            content_type="text/html",
            charset="utf-8",
        )
    return web.Response(
        status=200,
        text="<div class='grid-2'>" + "".join(out) + "</div>",
        content_type="text/html",
        charset="utf-8",
    )


async def dashboard_quality(request: web.Request) -> web.Response:
    config = request.app["config"]
    qdb = QualityDB(config["quality_db_path"])
    status = qdb.status()
    rows = []
    for provider, model in sorted(status.get("models", {}).keys()):
        s = status["models"][(provider, model)]
        rows.append(
            f"<tr><td><code>{_esc(provider)}</code></td>"
            f"<td><code>{_esc(model)}</code></td>"
            f"<td>{s['total_calls']}</td>"
            f"<td class='ok'>{s['success_calls']}</td>"
            f"<td class='err'>{s['failure_calls']}</td>"
            f"<td>{s['quality_score']:.1f}</td></tr>"
        )
    if not rows:
        return web.Response(
            status=200,
            text="<span class='meta'>no quality data yet</span>",
            content_type="text/html",
            charset="utf-8",
        )
    return web.Response(
        status=200,
        text=(
            "<table><thead><tr><th>provider</th><th>model</th><th>total</th><th>ok</th><th>err</th><th>score</th></tr></thead>"
            "<tbody>" + "".join(rows[:50]) + "</tbody></table>"
        ),
        content_type="text/html",
        charset="utf-8",
    )


__all__ = [
    "dashboard_handler",
    "dashboard_meta",
    "dashboard_pools",
    "dashboard_breakers",
    "dashboard_cooldowns",
    "dashboard_quota",
    "dashboard_quality",
]
