"""Endpoint handlers: /models, /chat/completions, /responses."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any

from aiohttp import web

from tusker_gateway.cache import ResponseCache, make_cache_key
from tusker_gateway.budget import BudgetTracker
from tusker_gateway.circuit_breaker import CircuitBreaker, BreakerDecision
from tusker_gateway.errors import BadRequestError, openai_error
from tusker_gateway.metrics import MetricsRegistry
from tusker_gateway.passthrough import PassthroughClient
from tusker_gateway.pools import PoolManager
from tusker_gateway.quality import QualityDB
from tusker_gateway.rate_limit import RateLimiter
from tusker_gateway.routing import resolve_route
from tusker_gateway.sse import (
    format_openai_chunk,
    sse_done,
    sse_frame,
    sse_heartbeat_loop,
)
from tusker_gateway.tracing import Tracer

logger = logging.getLogger(__name__)


# Heartbeat interval for client-facing SSE streams. Must be comfortably below
# the idle-connection timeouts of common intermediaries:
#   - Traefik default `Transport.RespondingTimeouts.IdleTimeout` = 60s
#   - nginx `proxy_read_timeout` (commonly)               = 60s
#   - Cloudflare free tier                                 = 100s
#   - CloudFront                                          = 5m (safe)
# 15s gives us at least 4 heartbeats before any of these fire.
#
# Read at request time (not import time) so tests can monkeypatch the
# env var without re-importing the module, and operators can flip the
# knob in production via the deployment manifest without a redeploy.
def _sse_heartbeat_secs() -> float:
    return float(os.environ.get("TUSKER_SSE_HEARTBEAT_SECS", "15"))


def _pool_name(body: dict[str, Any]) -> str | None:
    route = resolve_route(body.get("model"), body)
    return route.pool_name or "code" if route.kind in {"pool", "code"} else None


def _resolve_api_key(request: web.Request) -> str:
    """Return the raw bearer token used by the caller (for budget keying)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return ""


def _estimated_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough prompt-token estimate for budget pre-flight.

    We don't have tiktoken in this image, so we use a coarse char-based
    estimate (1 token ~= 4 chars). The pre-flight is intentionally
    conservative — over-budgeting a request by a few hundred tokens is
    fine, under-budgeting causes a 429 after the provider call which is
    worse.
    """
    chars = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    chars += len(str(part.get("text", "")))
    return max(1, chars // 4)


async def _call_with_pool_fallback(
    config: dict[str, Any],
    body: dict[str, Any],
    client: PassthroughClient,
    tools: list[dict[str, Any]] | None = None,
    breaker: CircuitBreaker | None = None,
) -> tuple[str, str, Any]:
    """Call a pool candidate, trying the next candidate after provider failure.

    If `breaker` is set, candidates whose breaker is OPEN are skipped before
    the call is attempted. Successful calls record success; failures (other
    than 429 rate-limit, which uses the cooldown path) record failure.
    """
    pool_name = _pool_name(body)
    if pool_name is None:
        provider, model = _route_target(config, body)
        decision = breaker.check(provider, model) if breaker else BreakerDecision(allowed=True, state=None)
        if not decision.allowed:
            raise BadRequestError(
                f"circuit open for {provider}/{model}: {decision.reason}",
                code="circuit_open",
            )
        try:
            result = await client.chat(provider, model, body["messages"], stream=bool(body.get("stream")), tools=tools)
            if breaker is not None:
                breaker.record_success(provider, model)
            return provider, model, result
        except Exception:
            if breaker is not None:
                breaker.record_failure(provider, model)
            raise

    excluded: set[tuple[str, str]] = set()
    last_error: Exception | None = None
    # Single shared PoolManager across retries so stickiness / cooldown
    # state is consistent within a single request.
    pool_mgr = PoolManager(config)
    while True:
        # Pool select() already filters out cooldown-blocked candidates;
        # additionally filter out breaker-open candidates here.
        selected = pool_mgr.select(pool_name, excluded=excluded)
        if breaker is not None and selected is not None:
            while selected is not None:
                decision = breaker.check(selected[0], selected[1])
                if decision.allowed:
                    break
                excluded.add(selected)
                selected = pool_mgr.select(pool_name, excluded=excluded)
        if not selected:
            if last_error is not None:
                raise last_error
            raise BadRequestError("No healthy models in pool", code="no_healthy_models")
        provider, model = selected
        try:
            result = await client.chat(provider, model, body["messages"], stream=bool(body.get("stream")), tools=tools)
            if breaker is not None:
                breaker.record_success(provider, model)
            return provider, model, result
        except Exception as exc:
            if breaker is not None:
                breaker.record_failure(provider, model)
            last_error = exc
            excluded.add(selected)


def _validate_chat_body(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BadRequestError("Request body must be a JSON object", code="invalid_request")
    if "messages" not in body:
        raise BadRequestError("messages is required", code="invalid_request")
    messages = body["messages"]
    if not isinstance(messages, list) or not messages:
        raise BadRequestError("messages must be a non-empty array", code="invalid_messages")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise BadRequestError("Each message must have a valid role", code="invalid_messages")
        if "content" not in message and message.get("role") != "assistant":
            raise BadRequestError("Each message must contain content", code="invalid_messages")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise BadRequestError("stream must be a boolean", code="invalid_stream")
    return body


def _route_target(config: dict[str, Any], body: dict[str, Any]) -> tuple[str, str]:
    route = resolve_route(body.get("model"), body)
    if route.kind in {"pool", "code"}:
        selected = PoolManager(config).select(route.pool_name or "code")
        if not selected:
            raise BadRequestError("No healthy models in pool", code="no_healthy_models")
        return selected
    if route.kind == "passthrough" and route.provider and route.model:
        return route.provider, route.model
    raise BadRequestError("Unsupported model route", code="unsupported_route")


async def models_handler(request: web.Request) -> web.Response:
    """GET /v1/models — list available models."""
    config = request.app["config"]
    data = [{"id": config["model_name"], "object": "model", "owned_by": "tusker-gateway"}]
    data.extend({"id": alias, "object": "model", "owned_by": "tusker-gateway"} for alias in ("hermes-code", "hermes-privacy", "hermes-premium", "hermes-swarm"))
    return web.json_response({"object": "list", "data": data})


async def metrics_handler(request: web.Request) -> web.Response:
    """GET /metrics — Prometheus text exposition."""
    metrics: MetricsRegistry | None = request.app.get("metrics")
    if metrics is None:
        return web.Response(status=404, text="metrics not enabled\n")
    # Refresh gauges from live state.
    cache: ResponseCache | None = request.app.get("cache")
    if cache is not None:
        s = cache.stats_snapshot()
        metrics.cache_hits._values[()] = float(s["hits"])  # noqa: SLF001
        metrics.cache_misses._values[()] = float(s["misses"])
        metrics.cache_writes._values[()] = float(s["writes"])
        metrics.cache_evictions._values[()] = float(s["evictions"])
    budget: BudgetTracker | None = request.app.get("budget")
    if budget is not None:
        s = budget.stats_snapshot()
        for kind, value in (
            ("daily", s["blocks_daily"]),
            ("monthly", s["blocks_monthly"]),
            ("pool", s["blocks_pool"]),
            ("global_daily", s["blocks_global"]),
        ):
            metrics.budget_blocks._values[(kind,)] = float(value)  # noqa: SLF001
        metrics.budget_records._values[()] = float(s["records"])
        metrics.budget_refunds._values[()] = float(s["refunds"])
    breaker: CircuitBreaker | None = request.app.get("breaker")
    if breaker is not None:
        s = breaker.stats_snapshot()
        # Surface breaker stats via existing budget_blocks counter family so
        # we don't grow the metric catalogue. Reuse 'breaker' kind label.
        for kind in ("trips", "short_circuits", "half_open_probes", "half_open_successes", "half_open_failures"):
            metrics.budget_blocks._values[(f"breaker_{kind}",)] = float(s[kind])  # noqa: SLF001
    ratelimit: RateLimiter | None = request.app.get("ratelimit")
    if ratelimit is not None:
        s = ratelimit.stats_snapshot()
        metrics.budget_blocks._values[("ratelimit_allowed",)] = float(s["allowed"])  # noqa: SLF001
        metrics.budget_blocks._values[("ratelimit_blocked",)] = float(s["blocked"])  # noqa: SLF001
    sem_cache = request.app.get("semantic_cache")
    if sem_cache is not None and sem_cache.enabled:
        s = sem_cache.stats_snapshot()
        for kind, value in (
            ("hits", s["hits"]),
            ("misses", s["misses"]),
            ("writes", s["writes"]),
            ("evictions", s["evictions"]),
        ):
            metrics.budget_blocks._values[(f"semantic_{kind}",)] = float(value)  # noqa: SLF001
    body = metrics.render()
    return web.Response(
        status=200,
        body=body,
        headers={"Content-Type": MetricsRegistry.CONTENT_TYPE},
    )


async def chat_completions_handler(request: web.Request) -> web.Response | web.StreamResponse:
    """POST /v1/chat/completions."""
    metrics: MetricsRegistry | None = request.app.get("metrics")
    cache: ResponseCache | None = request.app.get("cache")
    sem_cache = request.app.get("semantic_cache")
    budget: BudgetTracker | None = request.app.get("budget")
    breaker: CircuitBreaker | None = request.app.get("breaker")
    ratelimit: RateLimiter | None = request.app.get("ratelimit")
    tracer: Tracer | None = request.app.get("tracer")

    started = time.monotonic()
    pool_name = "passthrough"  # overwritten for pool-routed requests
    provider = "unknown"
    target_model = "unknown"
    status = "ok"
    body: dict[str, Any] | None = None
    api_key = _resolve_api_key(request)

    def _emit(status_label: str, provider_label: str | None = None,
              model_label: str | None = None) -> None:
        if metrics is None:
            return
        pl = provider_label if provider_label is not None else provider
        ml = model_label if model_label is not None else target_model
        metrics.requests_total.inc({"pool": pool_name, "provider": pl, "model": ml, "status": status_label})
        metrics.request_duration.observe(time.monotonic() - started, {"pool": pool_name, "provider": pl, "model": ml})

    # Top-level span (synchronous context).
    span_cm = (
        tracer.span("chat_completion", attributes={
            "http.method": request.method,
            "http.path": "/v1/chat/completions",
            "tusker.api_key_fingerprint": _resolve_api_key(request)[:16],
        })
        if tracer is not None and tracer.enabled
        else _noop_cm()
    )

    with span_cm as root_span:
        try:
            body = _validate_chat_body(await request.json())
            logger.info('chat request model=%s pool=%s stream=%s', body.get("model"), pool_name, body.get("stream"))
            if tracer is not None and tracer.enabled and root_span is not None:
                root_span.attributes["tusker.model"] = str(body.get("model") or "")
            config = request.app["config"]
            client = PassthroughClient(config, QualityDB(config["quality_db_path"]), request.app["http_session"])
            tools = body.get("tools") if isinstance(body.get("tools"), list) else None
            pool_name = _pool_name(body) or "passthrough"
            bypass_cache = request.headers.get("X-Tusker-Cache", "").strip().lower() == "bypass"

            # Rate-limit pre-flight (cheapest check, runs first).
            if ratelimit is not None and api_key:
                rl = ratelimit.check(api_key)
                if not rl.allowed:
                    status = "ratelimit_blocked"
                    if metrics is not None:
                        metrics.budget_blocks.inc({"kind": "ratelimit_blocked"})
                    _emit(status)
                    headers = {
                        "Retry-After": str(int(rl.retry_after) + 1),
                        "X-Tusker-RateLimit-Reason": rl.reason or "rate limit exceeded",
                    }
                    return web.json_response(
                        openai_error(rl.reason or "rate limit exceeded", code="rate_limit_error", error_type="rate_limit_error"),
                        status=429,
                        headers=headers,
                    )

            # Cache lookup
            cache_key: str | None = None
            if cache is not None and not body.get("stream", False) and not bypass_cache:
                cache_key = make_cache_key(
                    pool_name=pool_name,
                    model=body.get("model"),
                    messages=body["messages"],
                    tools=tools,
                    extra_body=body.get("extra_body"),
                )
                hit = cache.get(cache_key)
                if hit is not None:
                    logger.debug('cache hit key=%s', cache_key[:16])
                    if metrics is not None:
                        metrics.requests_total.inc(
                            {"pool": pool_name, "provider": "cache", "model": str(body.get("model") or ""), "status": "cache_hit"}
                        )
                        metrics.request_duration.observe(time.monotonic() - started, {"pool": pool_name, "provider": "cache", "model": str(body.get("model") or "")})
                    return web.json_response(hit)

            # Semantic cache lookup (after exact-match miss).
            sem_hit: dict[str, Any] | None = None
            if (
                sem_cache is not None
                and sem_cache.enabled
                and not body.get("stream", False)
                and not bypass_cache
            ):
                sem_hit = await sem_cache.query(body["messages"])
                if sem_hit is not None:
                    logger.info('semantic cache hit model=%s', body.get("model"))
                    if metrics is not None:
                        metrics.requests_total.inc(
                            {"pool": pool_name, "provider": "semantic_cache", "model": str(body.get("model") or ""), "status": "cache_hit"}
                        )
                        metrics.request_duration.observe(time.monotonic() - started, {"pool": pool_name, "provider": "semantic_cache", "model": str(body.get("model") or "")})
                    # Also store in exact-match cache for faster subsequent lookups.
                    if cache is not None and cache_key is not None:
                        cache.put(cache_key, sem_hit)
                    return web.json_response(sem_hit)

            # Budget pre-flight
            if budget is not None and api_key:
                est = _estimated_tokens(body["messages"])
                decision = budget.check(api_key, pool_name, est)
                if not decision.allowed:
                    status = "budget_blocked"
                    if metrics is not None:
                        metrics.budget_blocks.inc({"kind": decision.cap_name or "unknown"})
                    _emit(status)
                    headers = {"X-Tusker-Budget-Reason": decision.reason or "budget exceeded"}
                    return web.json_response(
                        openai_error(decision.reason or "budget exceeded", code="budget_exceeded", error_type="rate_limit_error"),
                        status=429,
                        headers=headers,
                    )

            provider, target_model, result = await _call_with_pool_fallback(config, body, client, tools, breaker=breaker)
            logger.debug('selected %s/%s for pool %s', provider, target_model, pool_name)

            if budget is not None and api_key and isinstance(result, dict):
                usage = result.get("usage") or {}
                used = int(usage.get("total_tokens") or _estimated_tokens(body["messages"]))
                budget.record(api_key, pool_name, used)

            if (
                cache is not None
                and not bypass_cache
                and not body.get("stream", False)
                and isinstance(result, dict)
                and cache_key is not None
            ):
                cache.put(cache_key, result)
                logger.debug('cache stored key=%s', cache_key[:16])

            # Store in semantic cache (non-streaming dict responses only).
            if (
                sem_cache is not None
                and sem_cache.enabled
                and not bypass_cache
                and not body.get("stream", False)
                and isinstance(result, dict)
            ):
                await sem_cache.store(body["messages"], result)
                logger.debug('semantic cache stored model=%s', body.get("model"))

            if body.get("stream", False):
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        # Disable nginx-style response buffering so SSE events
                        # flush immediately. Traefik honors this too.
                        "X-Accel-Buffering": "no",
                    },
                )
                await resp.prepare(request)

                # Send the role chunk *before* the first upstream byte. This
                # (a) gives the client a parseable first event immediately, and
                # (b) forces the first bytes through any proxy buffer so
                # subsequent heartbeats aren't held back. OpenAI's reference
                # streaming behavior starts with `delta: {role: "assistant"}`.
                await resp.write(sse_frame(format_openai_chunk(role="assistant")))

                stop = asyncio.Event()
                hb_interval = _sse_heartbeat_secs()
                hb_task = asyncio.create_task(
                    sse_heartbeat_loop(
                        resp.write,
                        stop,
                        interval_secs=hb_interval,
                        comment="keepalive",
                    ),
                    name="sse-heartbeat",
                )
                stream_ok = True
                try:
                    async for chunk in result:
                        await resp.write(chunk)
                except (ConnectionResetError, ConnectionError, BrokenPipeError) as exc:
                    stream_ok = False
                    logger.info(
                        "stream client disconnected mid-flight provider=%s model=%s err=%s",
                        provider, target_model, exc,
                    )
                    if budget is not None and api_key and body is not None:
                        budget.refund(api_key, pool_name, _estimated_tokens(body["messages"]))
                except asyncio.CancelledError:
                    stream_ok = False
                    logger.info(
                        "stream cancelled provider=%s model=%s", provider, target_model,
                    )
                    raise
                except Exception as exc:  # noqa: BLE001
                    stream_ok = False
                    logger.warning(
                        "stream pump failed provider=%s model=%s err=%s",
                        provider, target_model, exc,
                        exc_info=True,
                    )
                    if budget is not None and api_key and body is not None:
                        budget.refund(api_key, pool_name, _estimated_tokens(body["messages"]))
                else:
                    # Best-effort: if the client is gone, [DONE] write will
                    # raise — swallow so we still record metrics.
                    try:
                        await resp.write(sse_done())
                    except (ConnectionResetError, ConnectionError, BrokenPipeError):
                        stream_ok = False
                finally:
                    stop.set()
                    try:
                        await asyncio.wait_for(hb_task, timeout=hb_interval + 1.0)
                    except asyncio.TimeoutError:
                        hb_task.cancel()
                status = "ok" if stream_ok else status
                _emit(status)
                return resp

            _emit(status)
            if metrics is not None:
                usage = (result or {}).get("usage") or {} if isinstance(result, dict) else {}
                for direction, key in (("prompt", "prompt_tokens"), ("completion", "completion_tokens")):
                    n = int(usage.get(key) or 0)
                    if n:
                        metrics.tokens_total.inc({"pool": pool_name, "provider": provider, "model": target_model, "direction": direction}, n)
            return web.json_response(result)
        except BadRequestError as exc:
            status = exc.code or "bad_request"
            _emit(status)
            return web.json_response(openai_error(exc.message, code=exc.code, error_type=exc.error_type), status=exc.status)
        except Exception as exc:
            import traceback; logger.warning('chat request failed: %s\n%s', exc, traceback.format_exc())
            status = "provider_error"
            _emit(status)
            if budget is not None and api_key and body is not None:
                budget.refund(api_key, pool_name, _estimated_tokens(body["messages"]))
            return web.json_response(openai_error(str(exc), code="provider_error", error_type="provider_error"), status=502)


async def responses_handler(request: web.Request) -> web.Response | web.StreamResponse:
    try:
        body = await request.json()
        logger.info('responses request model=%s', body.get("model") if isinstance(body, dict) else None)
        if not isinstance(body, dict):
            raise BadRequestError("Request body must be a JSON object", code="invalid_request")
        input_value = body.get("input")
        if isinstance(input_value, str):
            messages = [{"role": "user", "content": input_value}]
        elif isinstance(input_value, list):
            messages = input_value
        else:
            raise BadRequestError("input must be a string or array", code="invalid_input")
        chat_body = {"model": body.get("model"), "messages": messages, "stream": bool(body.get("stream", False))}
        config = request.app["config"]
        client = PassthroughClient(config, QualityDB(config["quality_db_path"]), request.app["http_session"])
        _, _, result = await _call_with_pool_fallback(config, chat_body, client)
        if isinstance(result, dict) and "choices" in result:
            text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        else:
            text = ""
        resp_obj = {"id": f"resp_{uuid.uuid4().hex}", "object": "response", "created_at": int(time.time()), "model": body.get("model") or config["model_name"], "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}], "status": "completed"}
        return web.json_response(resp_obj)
    except BadRequestError as exc:
        return web.json_response(openai_error(exc.message, code=exc.code, error_type=exc.error_type), status=exc.status)
    except Exception as exc:
        return web.json_response(openai_error(str(exc), code="provider_error", error_type="provider_error"), status=502)


class _NoOpCM:
    """Null context manager that yields None — used when tracing is disabled."""

    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False


def _noop_cm():
    return _NoOpCM()
