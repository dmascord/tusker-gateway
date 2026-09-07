"""Anthropic Messages API adapter.

Dispatches ``POST /v1/messages`` through Tusker's passthrough pool,
translating Anthropic's request/response shapes via the registered
:mod:`tusker_gateway.translators` module. Format conversion is no longer
in this file — see ``translators/anthropic.py`` for the pure conversion
functions.

This module is HTTP glue: auth, budget/circuit/rate-limit preflight,
pool fallback, and SSE framing. It owns:

- ``anthropic_messages_handler`` — the aiohttp handler for ``/v1/messages``.
- ``_call_with_pool_fallback_anthropic`` — pool selection + breaker + dispatch.
- ``_AnthropicSSEStreamAdapter`` — adapts an upstream OpenAI byte stream
  to the registered Anthropic streaming translator (parses each OpenAI
  SSE event into a dict, hands it to the translator, yields the raw
  Anthropic SSE bytes).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncIterator

from aiohttp import web

from tusker_gateway import translators
from tusker_gateway.budget import BudgetTracker
from tusker_gateway.circuit_breaker import CircuitBreaker, BreakerDecision
from tusker_gateway.config import provider_route_is_disabled
from tusker_gateway.errors import (
    BadRequestError,
    NoHealthyModelsError,
    ProviderRouteDisabledError,
)
from tusker_gateway.endpoints import (
    _PreparedStream,
    _build_extra_body,
    _close_async_iterator,
    _complete_chat_result_stream,
    _normalize_stream,
    _prepare_stream_result,
    _quarantine_tool_response_failure,
    _request_conversation_id,
    _required_input_modalities,
    _tool_choice_requires_call,
    _validate_tool_choice_request,
)
from tusker_gateway.metrics import MetricsRegistry
from tusker_gateway.identity import (
    model_patterns_for_request,
    pool_allowed_for_request,
    provider_patterns_for_request,
)
from tusker_gateway.observability import set_access_log_context
from tusker_gateway.passthrough import PassthroughClient
from tusker_gateway.pools import PoolManager
from tusker_gateway.quality import QualityDB
from tusker_gateway.rate_limit import RateLimiter
from tusker_gateway.routing import resolve_route
from tusker_gateway.sse import sse_heartbeat_loop
from tusker_gateway.storage import StorageUnavailableError
from tusker_gateway.tool_formats import normalize_response_tool_calls
from tusker_gateway.tracing import Tracer

logger = logging.getLogger(__name__)

# Importing translators.anthropic registers the format converter on import.
from tusker_gateway.translators import ANTHROPIC  # noqa: F401
from tusker_gateway.translators.anthropic import (  # noqa: F401
    init_anthropic_stream_state,
    update_stream_state,
)


# ---------------------------------------------------------------------------
# HTTP-level helpers
# ---------------------------------------------------------------------------


def _anthropic_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Thin wrapper around the registry for the Anthropic → OpenAI request.

    Equivalent to ``translators.translate_request(ANTHROPIC, body)``; kept
    as a module-level alias because callers in this file already pass the
    body around as ``openai_body``.
    """
    return translators.translate_request(ANTHROPIC, body)


def _openai_to_anthropic(result: dict[str, Any], original_model: str) -> dict[str, Any]:
    """Convert a non-streaming OpenAI response to Anthropic format.

    Wraps ``translators.translate_response(ANTHROPIC, ...)`` and unwraps
    the single-element list (Anthropic responses are always one chunk).
    """
    chunks = translators.translate_response(
        ANTHROPIC, result, {"original_model": original_model},
    )
    return chunks[0] if chunks else {}


# ---------------------------------------------------------------------------
# Streaming adapter
# ---------------------------------------------------------------------------


def _parse_openai_sse_chunk(raw: bytes) -> dict[str, Any] | None:
    """Extract and parse the JSON ``data:`` payload from an OpenAI SSE line.

    Returns ``None`` for non-JSON frames and ``_OPENAI_SSE_DONE`` for the
    ``[DONE]`` sentinel. Keeping those cases distinct prevents a provider
    heartbeat or comment from closing an Anthropic stream prematurely.
    """
    try:
        text = raw.decode("utf-8").strip()
    except Exception:
        return None
    for line in text.split("\n"):
        if line.startswith("data: "):
            data_str = line[6:]
            if data_str == "[DONE]":
                return _OPENAI_SSE_DONE
            try:
                return json.loads(data_str)
            except json.JSONDecodeError:
                return None
    return None


_OPENAI_SSE_DONE = object()


class _AnthropicSSEStreamAdapter:
    """Adapts an upstream OpenAI byte stream to Anthropic SSE bytes.

    Uses the registered Anthropic streaming translator to convert each
    parsed OpenAI chunk into the appropriate ``content_block_delta`` (or
    message lifecycle) bytes. The wrapper is intentionally thin: HTTP
    framing, heartbeats, and error handling live in the calling handler.
    """

    def __init__(
        self,
        openai_stream: AsyncIterator[bytes],
        model: str,
        input_tokens: int = 0,
    ):
        self._stream = openai_stream.__aiter__()
        self._state = init_anthropic_stream_state()
        update_stream_state(
            self._state, model=model, input_tokens=input_tokens,
        )

    def __aiter__(self) -> "_AnthropicSSEStreamAdapter":
        return self

    async def aclose(self) -> None:
        await _close_async_iterator(self._stream)

    async def __anext__(self) -> bytes:
        # Pump the upstream until we have at least one non-empty frame to
        # emit (translators may return [] for chunks that have no content —
        # e.g. usage-only or finish-only frames).
        while True:
            try:
                raw = await self._stream.__anext__()
            except StopAsyncIteration:
                # Upstream closed — translate the close signal.
                frames = translators.stream_chunk(ANTHROPIC, None, self._state)
                if not frames:
                    raise StopAsyncIteration
                # Concatenate all frames so the caller sees them as one
                # logical SSE event burst.
                return b"".join(frames)

            parsed = _parse_openai_sse_chunk(raw)
            if parsed is _OPENAI_SSE_DONE:
                # [DONE] — emit the closing sequence.
                frames = translators.stream_chunk(ANTHROPIC, None, self._state)
                if not frames:
                    raise StopAsyncIteration
                return b"".join(frames)
            if parsed is None:
                # Comments/unknown frames carry no Anthropic content, but do
                # not imply that the upstream stream has ended.
                continue

            frames = translators.stream_chunk(ANTHROPIC, parsed, self._state)
            if frames:
                return b"".join(frames)
            # No output for this chunk — loop and pull the next one.


# Backwards-compatible aliases for callers (including tests) that imported
# the old module-level names. The actual implementations now live in
# ``tusker_gateway.translators.anthropic`` and are dispatched through the
# translator registry.
anthropic_to_openai = _anthropic_to_openai
openai_to_anthropic = _openai_to_anthropic
# Backwards-compatible alias for callers that imported the old class name.
# Internal use only — external callers should not depend on the class.
AnthropicSSEStreamTranslator = _AnthropicSSEStreamAdapter


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_api_key(request: web.Request) -> str:
    """Return the raw API key from Authorization or x-api-key header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    x_api_key = request.headers.get("x-api-key", "")
    return x_api_key.strip()


def _anthropic_error(message: str, *, type: str = "error") -> dict[str, Any]:
    """Build an Anthropic-compatible error response body."""
    return {
        "type": "error",
        "error": {
            "type": type,
            "message": message,
        },
    }


def _validate_anthropic_body(body: Any) -> dict[str, Any]:
    """Validate the Anthropic Messages API request body."""
    if not isinstance(body, dict):
        raise BadRequestError("Request body must be a JSON object", code="invalid_request")
    if "model" not in body:
        raise BadRequestError("model is required", code="invalid_request")
    if "messages" not in body:
        raise BadRequestError("messages is required", code="invalid_request")
    messages = body["messages"]
    if not isinstance(messages, list) or not messages:
        raise BadRequestError("messages must be a non-empty array", code="invalid_messages")
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise BadRequestError(f"messages[{idx}]: must be a JSON object", code="invalid_messages")
        role = msg.get("role")
        if role not in ("user", "assistant"):
            raise BadRequestError(
                f"messages[{idx}]: role must be 'user' or 'assistant', got '{role}'",
                code="invalid_messages",
            )
    if "max_tokens" not in body:
        raise BadRequestError("max_tokens is required", code="invalid_request")
    return body


def _pool_name_for_anthropic(model: str) -> str:
    """Determine pool name from an Anthropic model id."""
    route = resolve_route(model, {"model": model})
    if route.kind in {"pool", "code"} and route.pool_name:
        return route.pool_name
    return "passthrough"


def _sse_heartbeat_secs() -> float:
    return float(os.environ.get("TUSKER_SSE_HEARTBEAT_SECS", "15"))


def _estimated_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough prompt-token estimate for budget pre-flight."""
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return max(1, chars // 4)


class _NoOpCM:
    """Null context manager that yields None."""
    def __enter__(self):
        return None
    def __exit__(self, *a):
        return False


def _noop_cm():
    return _NoOpCM()


# ---------------------------------------------------------------------------
# Pool dispatch
# ---------------------------------------------------------------------------

async def _call_with_pool_fallback_anthropic(
    config: dict[str, Any],
    body: dict[str, Any],
    client: PassthroughClient,
    tools: list[dict[str, Any]] | None = None,
    breaker: CircuitBreaker | None = None,
    request: web.Request | None = None,
    conversation_id: str | None = None,
) -> tuple[str, str, Any]:
    """Dispatch OpenAI-format body through pool fallback.

    If the result is a streaming iterator, wraps it in
    ``_AnthropicSSEStreamAdapter`` to produce Anthropic SSE events.
    """
    model = body.get("model", "")
    route = resolve_route(model, body)

    if route.kind in {"pool", "code"} and route.pool_name:
        pool_name = route.pool_name
        excluded: set[tuple[str, str]] = set()
        last_error: Exception | None = None
        required_input_modalities = _required_input_modalities(body.get("messages"))
        requires_tools = bool(tools)
        pool_mgr = (
            request.app.get("pool_manager") or PoolManager(config)
            if request is not None
            else PoolManager(config)
        )
        configured_fallbacks = pool_mgr.fallback_pools(pool_name)
        if not isinstance(configured_fallbacks, (list, tuple)):
            configured_fallbacks = ()
        configured_fallbacks = tuple(
            fallback
            for fallback in configured_fallbacks
            if pool_allowed_for_request(request, fallback)
        )
        pool_names = [pool_name, *configured_fallbacks]
        pool_index = 0
        active_pool = pool_names[pool_index]
        while True:
            select_kwargs: dict[str, Any] = {
                "excluded": set(excluded),
                "required_input_modalities": required_input_modalities,
            }
            allowed_providers = provider_patterns_for_request(request)
            if allowed_providers is not None:
                select_kwargs["allowed_providers"] = allowed_providers
            allowed_models = model_patterns_for_request(request)
            if allowed_models is not None:
                select_kwargs["allowed_models"] = allowed_models
            if requires_tools:
                select_kwargs["requires_tools"] = True
            selected = await asyncio.to_thread(
                pool_mgr.select, active_pool, **select_kwargs
            )
            if breaker is not None and selected is not None:
                while selected is not None:
                    decision = await asyncio.to_thread(
                        breaker.check, selected[0], selected[1]
                    )
                    if decision.allowed:
                        break
                    excluded.add(selected)
                    select_kwargs = {
                        "excluded": set(excluded),
                        "required_input_modalities": required_input_modalities,
                    }
                    if allowed_providers is not None:
                        select_kwargs["allowed_providers"] = allowed_providers
                    if allowed_models is not None:
                        select_kwargs["allowed_models"] = allowed_models
                    if requires_tools:
                        select_kwargs["requires_tools"] = True
                    selected = await asyncio.to_thread(
                        pool_mgr.select, active_pool, **select_kwargs
                    )
            if not selected:
                if pool_index + 1 < len(pool_names):
                    previous_pool = active_pool
                    pool_index += 1
                    active_pool = pool_names[pool_index]
                    logger.warning(
                        "anthropic pool exhausted requested_pool=%s exhausted_pool=%s "
                        "fallback_pool=%s",
                        pool_name,
                        previous_pool,
                        active_pool,
                    )
                    continue
                if last_error is not None:
                    raise last_error
                raise NoHealthyModelsError(pool=pool_name)
            prov, mdl = selected
            try:
                result = await client.chat(prov, mdl, body["messages"],
                                          stream=bool(body.get("stream")), tools=tools,
                                          tool_choice=body.get("tool_choice"),
                                          extra_body=_build_extra_body(body) or None,
                                          conversation_id=conversation_id)
                if isinstance(result, dict):
                    result = normalize_response_tool_calls(
                        result,
                        source=f"{prov}/{mdl}",
                    )
                result = await _prepare_stream_result(
                    result,
                    provider=prov,
                    model=mdl,
                    request_id=(request.get("_request_id") if request is not None else None),
                    tools_requested=(
                        bool(body.get("stream"))
                        and (bool(tools) or body.get("tool_choice") == "none")
                    ),
                    tools=tools,
                    require_tool_call=_tool_choice_requires_call(body.get("tool_choice")),
                    tool_choice=body.get("tool_choice"),
                )
                if breaker is not None:
                    await asyncio.to_thread(breaker.record_success, prov, mdl)
                if body.get("stream"):
                    if isinstance(result, dict):
                        # Codex/Responses providers are parsed into a complete
                        # Chat result even when the caller requested a stream.
                        # Re-encode it before the Anthropic SSE adapter; a dict
                        # is iterable over keys and would otherwise produce an
                        # empty successful stream.
                        result = _complete_chat_result_stream(result)
                    if not hasattr(result, "__aiter__"):
                        raise BadRequestError(
                            "streaming Anthropic output is unavailable from the provider",
                            code="unsupported_streaming",
                        )
                    openai_stream = (
                        result
                        if isinstance(result, _PreparedStream)
                        else _normalize_stream(
                            result,
                            provider=prov,
                            model=mdl,
                        )
                    )
                    result = _AnthropicSSEStreamAdapter(
                        openai_stream,
                        model=model,
                        input_tokens=_estimated_tokens(body.get("messages", [])),
                    )
                return prov, mdl, result
            except Exception as exc:
                _quarantine_tool_response_failure(config, prov, mdl, exc)
                if breaker is not None:
                    await asyncio.to_thread(breaker.record_failure, prov, mdl)
                last_error = exc
                excluded.add(selected)
    elif route.kind == "passthrough" and route.provider and route.model:
        if provider_route_is_disabled(config, route.provider):
            raise ProviderRouteDisabledError(route.provider)
        decision = (
            await asyncio.to_thread(breaker.check, route.provider, route.model)
            if breaker
            else BreakerDecision(allowed=True, state=None)
        )
        if not decision.allowed:
            raise BadRequestError(
                f"circuit open for {route.provider}/{route.model}: {decision.reason}",
                code="circuit_open",
            )
        try:
            result = await client.chat(route.provider, route.model, body["messages"],
                                      stream=bool(body.get("stream")), tools=tools,
                                      tool_choice=body.get("tool_choice"),
                                      extra_body=_build_extra_body(body) or None,
                                      conversation_id=conversation_id)
            if isinstance(result, dict):
                result = normalize_response_tool_calls(
                    result,
                    source=f"{route.provider}/{route.model}",
                )
            result = await _prepare_stream_result(
                result,
                provider=route.provider,
                model=route.model,
                request_id=(request.get("_request_id") if request is not None else None),
                tools_requested=(
                    bool(body.get("stream"))
                    and (bool(tools) or body.get("tool_choice") == "none")
                ),
                tools=tools,
                require_tool_call=_tool_choice_requires_call(body.get("tool_choice")),
                tool_choice=body.get("tool_choice"),
            )
            if breaker is not None:
                await asyncio.to_thread(
                    breaker.record_success, route.provider, route.model
                )
            if body.get("stream"):
                if isinstance(result, dict):
                    result = _complete_chat_result_stream(result)
                if not hasattr(result, "__aiter__"):
                    raise BadRequestError(
                        "streaming Anthropic output is unavailable from the provider",
                        code="unsupported_streaming",
                    )
                openai_stream = (
                    result
                    if isinstance(result, _PreparedStream)
                    else _normalize_stream(
                        result,
                        provider=route.provider,
                        model=route.model,
                    )
                )
                result = _AnthropicSSEStreamAdapter(
                    openai_stream,
                    model=model,
                    input_tokens=_estimated_tokens(body.get("messages", [])),
                )
            return route.provider, route.model, result
        except Exception as exc:
            _quarantine_tool_response_failure(
                config,
                route.provider,
                route.model,
                exc,
            )
            if breaker is not None:
                await asyncio.to_thread(
                    breaker.record_failure, route.provider, route.model
                )
            raise
    else:
        raise BadRequestError("Unsupported model route", code="unsupported_route")


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

async def anthropic_messages_handler(request: web.Request) -> web.Response | web.StreamResponse:
    """POST /v1/messages — Anthropic Messages API endpoint."""
    metrics: MetricsRegistry | None = request.app.get("metrics")
    budget: BudgetTracker | None = request.app.get("budget")
    breaker: CircuitBreaker | None = request.app.get("breaker")
    ratelimit: RateLimiter | None = request.app.get("ratelimit")
    tracer: Tracer | None = request.app.get("tracer")

    started = time.monotonic()
    pool_name = "passthrough"
    provider = "unknown"
    target_model = "unknown"
    status = "ok"
    body: dict[str, Any] | None = None
    budget_recorded = False
    budget_charged = 0
    openai_body: dict[str, Any] = {}
    api_key = _resolve_api_key(request)
    original_model = ""

    def _emit(status_label: str, provider_label: str | None = None,
              model_label: str | None = None) -> None:
        if metrics is None:
            return
        pl = provider_label if provider_label is not None else provider
        ml = model_label if model_label is not None else target_model
        metrics.requests_total.inc({"pool": pool_name, "provider": pl, "model": ml, "status": status_label})
        metrics.request_duration.observe(time.monotonic() - started, {"pool": pool_name, "provider": pl, "model": ml})

    span_cm = (
        tracer.span("anthropic_message", attributes={
            "http.method": request.method,
            "http.path": "/v1/messages",
            "tusker.api_key_fingerprint": api_key[:16],
        })
        if tracer is not None and tracer.enabled
        else _noop_cm()
    )

    with span_cm:
        try:
            body = _validate_anthropic_body(await request.json())
            original_model = body.get("model", "")
            logger.info("anthropic request model=%s stream=%s", original_model, body.get("stream"))

            # Convert to OpenAI format.
            openai_body = _anthropic_to_openai(body)
            _validate_tool_choice_request(
                openai_body.get("tool_choice"),
                openai_body.get("tools"),
            )
            conversation_body = {
                **body,
                "messages": openai_body.get("messages", []),
            }
            conversation_id = _request_conversation_id(
                request,
                conversation_body,
                api_key,
            )

            config = request.app["config"]
            client = PassthroughClient(
                config,
                request.app.get("quality_db") or QualityDB(config["quality_db_path"]),
                request.app["http_session"],
                catalog_registry=request.app.get("catalog_registry"),
                credential_rotators=request.app.get("credential_rotators"),
            )
            tools = openai_body.pop("tools", None)
            pool_name = _pool_name_for_anthropic(original_model)
            set_access_log_context(request, pool=pool_name)

            # Rate-limit pre-flight.
            if ratelimit is not None and api_key:
                try:
                    rl = await asyncio.to_thread(ratelimit.check, api_key)
                except StorageUnavailableError:
                    status = "state_store_unavailable"
                    _emit(status)
                    return web.json_response(
                        _anthropic_error(
                            "Gateway state storage is temporarily unavailable",
                            type="server_error",
                        ),
                        status=503,
                        headers={"Retry-After": "2"},
                    )
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
                        _anthropic_error(rl.reason or "rate limit exceeded", type="rate_limit_error"),
                        status=429,
                        headers=headers,
                    )

            # Budget pre-flight.
            if budget is not None and api_key:
                est = _estimated_tokens(openai_body.get("messages", []))
                try:
                    decision = await asyncio.to_thread(
                        budget.check, api_key, pool_name, est
                    )
                except StorageUnavailableError:
                    status = "state_store_unavailable"
                    _emit(status)
                    return web.json_response(
                        _anthropic_error(
                            "Gateway state storage is temporarily unavailable",
                            type="server_error",
                        ),
                        status=503,
                        headers={"Retry-After": "2"},
                    )
                if not decision.allowed:
                    status = "budget_blocked"
                    if metrics is not None:
                        metrics.budget_blocks.inc({"kind": decision.cap_name or "unknown"})
                    _emit(status)
                    return web.json_response(
                        _anthropic_error(decision.reason or "budget exceeded", type="rate_limit_error"),
                        status=429,
                    )

            # Dispatch to backend.
            provider, target_model, result = await _call_with_pool_fallback_anthropic(
                config, openai_body, client, tools, breaker=breaker, request=request,
                conversation_id=conversation_id,
            )
            logger.debug("selected %s/%s for anthropic model=%s", provider, target_model, original_model)
            set_access_log_context(
                request,
                provider=provider,
                model=target_model,
                pool=pool_name,
            )

            # Budget recording.
            if budget is not None and api_key and isinstance(result, dict):
                usage = result.get("usage") or {}
                used = int(usage.get("total_tokens") or _estimated_tokens(openai_body.get("messages", [])))
                await asyncio.to_thread(budget.record, api_key, pool_name, used)
                budget_recorded = True
                budget_charged = used

            # Streaming response.
            if body.get("stream"):
                if budget is not None and api_key and not budget_recorded:
                    await asyncio.to_thread(
                        budget.record,
                        api_key,
                        pool_name,
                        _estimated_tokens(openai_body.get("messages", [])),
                    )
                    budget_recorded = True
                    budget_charged = _estimated_tokens(openai_body.get("messages", []))
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Request-ID": request.get("_request_id", ""),
                        "X-Accel-Buffering": "no",
                    },
                )
                await resp.prepare(request)

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
                    status = "client_disconnected"
                    request["_stream_error"] = status
                    logger.info(
                        "anthropic stream client disconnected provider=%s model=%s err=%s",
                        provider, target_model, exc,
                    )
                    if budget_recorded and budget is not None and api_key:
                        await asyncio.to_thread(budget.refund, api_key, pool_name, budget_charged)
                        budget_recorded = False
                except asyncio.CancelledError:
                    stream_ok = False
                    status = "stream_cancelled"
                    request["_stream_error"] = status
                    if budget_recorded and budget is not None and api_key:
                        await asyncio.to_thread(budget.refund, api_key, pool_name, budget_charged)
                        budget_recorded = False
                    raise
                except Exception as exc:  # noqa: BLE001
                    stream_ok = False
                    status = "upstream_stream_error"
                    request["_stream_error"] = status
                    logger.warning(
                        "anthropic stream pump failed provider=%s model=%s err=%s",
                        provider, target_model, exc,
                        exc_info=True,
                    )
                    if budget_recorded and budget is not None and api_key:
                        await asyncio.to_thread(budget.refund, api_key, pool_name, budget_charged)
                        budget_recorded = False
                finally:
                    await _close_async_iterator(result)
                    stop.set()
                    try:
                        await asyncio.wait_for(hb_task, timeout=hb_interval + 1.0)
                    except asyncio.TimeoutError:
                        hb_task.cancel()
                status = "ok" if stream_ok else status
                _emit(status)
                return resp

            # Non-streaming: convert OpenAI → Anthropic format.
            anthropic_resp = _openai_to_anthropic(result, original_model)
            _emit(status)
            if metrics is not None:
                usage = (result or {}).get("usage") or {} if isinstance(result, dict) else {}
                for direction, key in (("prompt", "prompt_tokens"), ("completion", "completion_tokens")):
                    n = int(usage.get(key) or 0)
                    if n:
                        metrics.tokens_total.inc({"pool": pool_name, "provider": provider, "model": target_model, "direction": direction}, n)
            return web.json_response(anthropic_resp)

        except BadRequestError as exc:
            status = exc.code or "bad_request"
            _emit(status)
            return web.json_response(
                _anthropic_error(exc.message, type=exc.error_type),
                status=exc.status,
                headers=getattr(exc, "headers", None),
            )
        except Exception as exc:
            logger.warning("anthropic request failed: %s", exc)
            status = "provider_error"
            _emit(status)
            if budget_recorded and budget is not None and api_key and body is not None:
                await asyncio.to_thread(budget.refund, api_key, pool_name, budget_charged)
            return web.json_response(
                _anthropic_error(str(exc), type="api_error"),
                status=502,
            )
