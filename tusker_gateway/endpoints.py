"""Endpoint handlers: /models, /chat/completions, /responses."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncIterator

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


_TOOL_CALL_XML_RE = re.compile(
    r"<\|?\s*/?\s*tool_call\s*\|?>|<function=[\s\S]*?</function>",
    re.IGNORECASE,
)

# Opening tokens that mark the start of a tool-call block. When the tail of
# the accumulated buffer matches one of these prefixes, we hold it back until
# the full block has arrived (a block may span several streamed chunks).
_TOOL_OPENERS = (
    "<tool_call>",
    "<tool_call",
    "<|tool_call|>",
    "<|tool_call",
    "<|start_header|>",
    "<function=",
    "<|begin_of_",
)


class _ToolCallStripper:
    """Stateful stripper that removes XML/Markdown tool_call markup from a
    streamed text sequence, tolerating block boundaries that split across
    network chunks.

    Holds a ``_carry`` tail that may be an incomplete opening token; when a
    complete block is seen it is dropped, otherwise carried text is emitted.
    """

    def __init__(self) -> None:
        self._carry = ""

    def _looks_like_opener(self, text: str) -> bool:
        """Return True if `text` is a prefix of a known tool-call opener."""
        if not text:
            return False
        lower = text.lower()
        for opener in _TOOL_OPENERS:
            if opener.startswith(lower):
                return True
        return False

    def feed(self, chunk: str) -> str:
        """Process a content chunk, returning the clean (emit-able) text."""
        if not chunk:
            return ""
        text = self._carry + chunk
        self._carry = ""
        out: list[str] = []

        # Repeatedly remove complete tool-call blocks.
        while True:
            m = _TOOL_CALL_XML_RE.search(text)
            if not m:
                break
            out.append(text[: m.start()])
            text = text[m.end():]

        # Now `text` contains no complete tool block. Check whether its
        # trailing suffix is an incomplete opener prefix that may continue
        # into the next chunk; carry it if so, otherwise emit it.
        if text:
            emit_end = len(text)
            carry = ""
            for i in range(len(text)):
                tail = text[i:]
                if self._looks_like_opener(tail):
                    emit_end = i
                    carry = tail
                    break
            out.append(text[:emit_end])
            self._carry = carry
        return "".join(out)

    def flush(self) -> str:
        """Drop any remaining carried content (partial opener that never
        completed). Returns the clean emitted text, usually empty."""
        carry, self._carry = self._carry, ""
        return ""


def _strip_xml_tool_calls(content: str) -> str:
    """Strip XML/Markdown-style tool_call markup from assistant content.

    Some open-source models (e.g. DeepSeek, Qwen) emit tool-call markup
    as raw text in the content stream even when tools are provided via
    the structured `tool_calls` API field. This causes clients like
    OMP to see duplicate tool-call artifacts ("text tool calls leaking
    through"). We strip both the wrapper tags and the inner function
    payloads so the content stream only carries the prose.

    We only strip when the markup contains tool-call-shaped tags so normal
    text mentioning "tool_call" is preserved.
    """
    if not content or "<" not in content:
        return content
    return _TOOL_CALL_XML_RE.sub("", content)


async def _normalize_stream(raw_stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Normalize upstream SSE chunks for OMP/client compatibility.

    Some upstream providers bundle `delta.content` and `finish_reason` in the
    same SSE chunk. OMP (and other strict OpenAI clients) require:
      - content-only delta chunks with finish_reason: null
      - a separate final chunk with finish_reason set
    This generator splits bundled chunks into separate SSE frames.

    Additionally, some open-source models emit XML/Markdown-style
    ``<tool_call>...<function=...>...</function></tool_call>`` markup in
    the content stream alongside structured ``tool_calls`` deltas. This
    shows up in OMP as raw text tool calls "leaking through". We strip
    that markup from content deltas so OMP sees only the structured
    tool_calls and the surrounding prose.

    The upstream yields arbitrary byte chunks from `resp.content.iter_any()`,
    which may contain multiple SSE events. We split on ``\\n\\n`` boundaries,
    process each ``data:`` event individually, and re-emit them.
    """
    buffer = b""
    tool_stripper = _ToolCallStripper()
    saw_finish_reason = False
    saw_done = False

    def _finish_frame() -> bytes:
        return sse_frame(format_openai_chunk(finish_reason="stop"))

    async for chunk in raw_stream:
        buffer += chunk
        while b"\n\n" in buffer:
            frame, buffer = buffer.split(b"\n\n", 1)
            frame += b"\n\n"  # preserve terminator for yield
            stripped = frame.strip()
            # Pass through non-data frames as-is (comments, empty)
            if not stripped.startswith(b"data: "):
                yield frame
                continue
            # Pass through [DONE] sentinel as-is
            if stripped == b"data: [DONE]":
                tool_stripper.flush()
                saw_done = True
                yield frame
                continue
            try:
                obj = json.loads(stripped[len(b"data: "):])
            except (json.JSONDecodeError, UnicodeDecodeError):
                yield frame
                continue
            choices = obj.get("choices")
            if not isinstance(choices, list) or not choices:
                yield frame
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            fr = choice.get("finish_reason")
            if fr:
                saw_finish_reason = True
            raw_content = delta.get("content")
            reasoning_content = delta.get("reasoning_content")
            # Some reasoning models (qwen, etc.) emit reasoning/thinking in
            # `reasoning_content` while leaving `content` null or empty.
            # OMP treats content=null as "no text" and ends the turn early.
            # Promote reasoning_content to content when content is absent so
            # clients see real text and keep the conversation alive.
            if raw_content is None and reasoning_content is not None:
                raw_content = reasoning_content
                delta["content"] = reasoning_content
                # OMP client compatibility: sometimes reasoning models omit "content"
                # when "reasoning_content" is present.
                if "reasoning_content" in delta:
                    del delta["reasoning_content"]
            has_content = isinstance(raw_content, str) and bool(raw_content)
            tc = delta.get("tool_calls")
            has_tools = bool(tc) and isinstance(tc, list) and len(tc) > 0
            # Strip XML/Markdown tool-call markup from content deltas using
            # a stateful stripper so markup spanning multiple streamed chunks
            # is still recognized and dropped.
            if has_content and "<" in raw_content:
                cleaned = tool_stripper.feed(raw_content)
                if not cleaned and has_tools:
                    delta = {k: v for k, v in delta.items() if k != "content"}
                else:
                    delta = {**delta, "content": cleaned}
            # Replace delta in choice and obj for downstream re-emission.
            new_choice = {**choice, "delta": delta}
            new_obj = {**obj, "choices": [new_choice, *choices[1:]]}
            # Split if chunk has BOTH content/tools AND finish_reason
            if fr and (bool(delta.get("content")) or has_tools):
                if bool(delta.get("content")):
                    content_delta = {k: v for k, v in delta.items()
                                   if k not in ("role", "tool_calls")}
                    content_obj = {**new_obj, "choices": [{**new_choice, "delta": content_delta, "finish_reason": None}]}
                    yield f"data: {json.dumps(content_obj, ensure_ascii=False)}\n\n".encode()
                if has_tools:
                    tools_only = {"role": delta.get("role"), "tool_calls": tc}
                    tools_obj = {**new_obj, "choices": [{**new_choice, "delta": tools_only, "finish_reason": None}]}
                    yield f"data: {json.dumps(tools_obj, ensure_ascii=False)}\n\n".encode()
                finish_obj = {**new_obj, "choices": [{**new_choice, "delta": {}, "finish_reason": fr}]}
                yield f"data: {json.dumps(finish_obj, ensure_ascii=False)}\n\n".encode()
            else:
                yield f"data: {json.dumps(new_obj, ensure_ascii=False)}\n\n".encode()
    # Flush any remaining partial frame in the buffer
    if buffer.strip():
        yield buffer
    # If the upstream ended without ever emitting a finish_reason, OMP
    # surfaces this as "stream closed before a finish_reason was received".
    # Synthesize a stop chunk so the client always has a clean termination.
    # Skip if the upstream already sent its own [DONE] (which implies it
    # terminated cleanly) to avoid emitting a chunk after the sentinel.
    if not saw_finish_reason and not saw_done:
        yield _finish_frame()


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


# Request fields handled separately by the gateway. Everything else from
# the request body is forwarded upstream as-is so callers can pass
# max_tokens / temperature / top_p / stop / seed / response_format / etc.
# without us needing to maintain a whitelist. Without this passthrough,
# upstream providers fall back to their own tiny defaults (often 256-512
# tokens) and the model silently truncates mid-task with a StopReason
# of 'length' that OMP then interprets as 'finished'.
_GATEWAY_HANDLED_FIELDS = frozenset({
    "model", "messages", "stream", "tools", "tool_choice",
})


def _build_extra_body(body: dict[str, Any]) -> dict[str, Any]:
    """Extract passthrough fields from a request body.

    Returns a dict of fields that should be forwarded to the upstream
    provider as `extra_body`, excluding fields the gateway already
    handles (model/messages/stream/tools/tool_choice).

    Modern OpenAI clients send `max_completion_tokens` while older
    providers and the codex Responses API only support `max_tokens`.
    Map the newer name to the older one so requests don't get rejected.
    """
    extra = {k: v for k, v in body.items() if k not in _GATEWAY_HANDLED_FIELDS}
    if "max_completion_tokens" in extra and "max_tokens" not in extra:
        extra["max_tokens"] = extra.pop("max_completion_tokens")
    return extra


async def _call_with_pool_fallback(
    config: dict[str, Any],
    body: dict[str, Any],
    client: PassthroughClient,
    tools: list[dict[str, Any]] | None = None,
    breaker: CircuitBreaker | None = None,
    request: web.Request | None = None,
) -> tuple[str, str, Any]:
    """Call a pool candidate, trying the next candidate after provider failure.

    If `breaker` is set, candidates whose breaker is OPEN are skipped before
    the call is attempted. Successful calls record success; failures (other
    than 429 rate-limit, which uses the cooldown path) record failure.
    """
    extra_body = _build_extra_body(body)
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
            result = await client.chat(
                provider, model, body["messages"],
                stream=bool(body.get("stream")),
                tools=tools,
                extra_body=extra_body or None,
            )
            if breaker is not None:
                breaker.record_success(provider, model)
            return provider, model, result
        except Exception:
            if breaker is not None:
                breaker.record_failure(provider, model)
            raise

    excluded: set[tuple[str, str]] = set()
    last_error: Exception | None = None
    # Prefer the app-level PoolManager (so catalog refresh + session
    # stickiness are shared); fall back to a per-request instance.
    if request is not None:
        pool_mgr = request.app.get("pool_manager") or PoolManager(config)
    else:
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
            result = await client.chat(
                provider, model, body["messages"],
                stream=bool(body.get("stream")),
                tools=tools,
                extra_body=extra_body or None,
            )
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
    request_id = uuid.uuid4().hex[:12]
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
            logger.info('chat request rid=%s model=%s pool=%s stream=%s', request_id, body.get("model"), pool_name, body.get("stream"))
            if tracer is not None and tracer.enabled and root_span is not None:
                root_span.attributes["tusker.model"] = str(body.get("model") or "")
            config = request.app["config"]
            client = PassthroughClient(config, QualityDB(config["quality_db_path"]), request.app["http_session"])
            tools = body.get("tools") if isinstance(body.get("tools"), list) else None
            pool_name = _pool_name(body) or "passthrough"
            bypass_cache = request.headers.get("X-Tusker-Cache", "").strip().lower() == "bypass"

            # Guard pipeline: input/output guards.
            guard_pipeline = request.app.get("guard_pipeline")
            if guard_pipeline is not None:
                guard_result = await guard_pipeline.run(body)
                if not guard_result.allowed:
                    status = "guardrail_blocked"
                    if metrics is not None:
                        metrics.guardrail_blocks.inc({"kind": guard_result.message or "blocked"})
                    _emit(status)
                    return web.json_response(
                        openai_error(guard_result.message or "request blocked by guardrail", code="guardrail_blocked", error_type="invalid_request_error"),
                        status=400,
                    )
                if guard_result.modified_body is not None:
                    body = guard_result.modified_body


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

            provider, target_model, result = await _call_with_pool_fallback(config, body, client, tools, breaker=breaker, request=request)
            logger.debug('selected rid=%s provider=%s model=%s pool=%s', request_id, provider, target_model, pool_name)

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
                    if isinstance(result, dict):
                        # Convert complete response to SSE chunks.
                        # Codex parses the full response from its SSE stream;
                        # the gateway receives it as a single dict and must
                        # emit it as proper OpenAI streaming chunks so the
                        # client (e.g. OMP) can consume them as text deltas.
                        choices = result.get("choices", [{}])
                        choice = choices[0]
                        message = choice.get("message", {})
                        content = message.get("content", "")
                        finish_reason = choice.get("finish_reason", "stop")
                        
                        # Emit content chunk if there's text
                        if content:
                            await resp.write(sse_frame(format_openai_chunk(content=content)))
                        
                        # Emit tool_calls as individual deltas if present
                        tool_calls = message.get("tool_calls")
                        if tool_calls:
                            for tc in tool_calls:
                                tc_id = tc.get("id", "")
                                fn = tc.get("function", {})
                                tc_delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": tc_id, "type": "function", "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments", "")}}]}
                                await resp.write(sse_frame({"id": result.get("id", "chatcmpl-tusker"), "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": tc_delta}], "model": result.get("model", "tusker-gateway")}))
                        
                        # Emit finish_reason chunk (distinct from content to satisfy OMP)
                        await resp.write(sse_frame(format_openai_chunk(finish_reason=finish_reason)))
                    else:
                        async for chunk in _normalize_stream(result):
                            await resp.write(chunk)
                except (ConnectionResetError, ConnectionError, BrokenPipeError) as exc:
                    stream_ok = False
                    logger.info(
                        "stream client disconnected mid-flight rid=%s provider=%s model=%s err=%s",
                        request_id, provider, target_model, exc,
                    )
                    if budget is not None and api_key and body is not None:
                        budget.refund(api_key, pool_name, _estimated_tokens(body["messages"]))
                except asyncio.CancelledError:
                    stream_ok = False
                    logger.info(
                        "stream cancelled rid=%s provider=%s model=%s", request_id, provider, target_model,
                    )
                    raise
                except Exception as exc:  # noqa: BLE001
                    stream_ok = False
                    logger.warning(
                        "stream pump failed rid=%s provider=%s model=%s err=%s",
                        request_id, provider, target_model, exc,
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
            logger.warning('chat request failed rid=%s: %s', request_id, exc)
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
        _, _, result = await _call_with_pool_fallback(config, chat_body, client, request=request)
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


async def images_handler(request: web.Request) -> web.Response:
    """POST /v1/images/generations, /v1/images/edits, /v1/images/variations.

    Image generation endpoint for OpenAI GPT Image models and other providers.
    Delegates to the ImageGenerationHandler for routing and processing.
    """
    try:
        body = await request.json()
        model = body.get("model", "gpt-image-2")

        image_handler = request.app.get("image_handler")
        if image_handler is None:
            return web.json_response(
                openai_error("image handler not initialised", code="internal_error", error_type="internal"),
                status=503,
            )

        provider = image_handler.get_provider_for_image_request(model, request.path)
        config = request.app["config"]
        provider_keys = config.get("provider_api_keys", {})
        api_key = provider_keys.get(provider)

        codex_rotator = request.app.get("codex_rotator")
        result = await image_handler.handle_request(
            model=model,
            path=request.path,
            body=body,
            api_key=api_key,
            codex_rotator=codex_rotator,
        )
        return web.json_response(result)

    except Exception as exc:
        logger.warning("Image generation request failed: %s", exc)
        return web.json_response(
            openai_error(str(exc), code="image_generation_error", error_type="provider_error"),
            status=502,
        )


async def tts_handler(request: web.Request) -> web.Response:
    """POST /v1/audio/speech.

    Text-to-speech endpoint. Returns binary audio (mp3/pcm/opus/...) with the
    upstream's Content-Type. Dispatches to OpenAI when an OPENAI_API_KEY is
    configured, otherwise to OpenRouter.
    """
    try:
        body = await request.json()
        model = body.get("model", "tts-1")
        tts = request.app.get("tts_handler")
        if tts is None:
            return web.json_response(
                openai_error("tts handler not initialised", code="internal_error", error_type="internal"),
                status=503,
            )
        config = request.app["config"]
        provider_keys = config.get("provider_api_keys", {})
        provider = tts.get_provider_for_tts_request(model)
        api_key = provider_keys.get(provider)
        audio_bytes, content_type = await tts.handle_request(
            model=model,
            body=body,
            api_key=api_key,
        )
        return web.Response(body=audio_bytes, content_type=content_type)
    except Exception as exc:
        logger.warning("TTS request failed: %s", exc)
        return web.json_response(
            openai_error(str(exc), code="tts_error", error_type="provider_error"),
            status=502,
        )


async def video_handler(request: web.Request) -> web.Response:
    """POST /v1/videos.

    Video generation endpoint. Returns a JSON job object. When wait=true
    (default) the gateway polls the upstream until the job completes and
    includes the rendered MP4 as base64 under b64_json. Set wait=false to
    get the initial job object immediately.
    """
    try:
        body = await request.json()
        model = body.get("model", "sora-2")
        wait = _truthy(request.query.get("wait", "true"))
        video = request.app.get("video_handler")
        if video is None:
            return web.json_response(
                openai_error("video handler not initialised", code="internal_error", error_type="internal"),
                status=503,
            )
        config = request.app["config"]
        provider_keys = config.get("provider_api_keys", {})
        provider = video.get_provider_for_video_request(model)
        api_key = provider_keys.get(provider)
        result = await video.handle_request(
            model=model,
            body=body,
            api_key=api_key,
            wait=wait,
        )
        return web.json_response(result)
    except Exception as exc:
        logger.warning("Video request failed: %s", exc)
        return web.json_response(
            openai_error(str(exc), code="video_error", error_type="provider_error"),
            status=502,
        )


def _truthy(value: str) -> bool:
    """Parse a query-string bool. False for anything other than 1/true/yes/on."""
    return value.lower() in ("1", "true", "yes", "on")
