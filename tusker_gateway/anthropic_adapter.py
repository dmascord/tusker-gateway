"""Anthropic Messages API adapter.

Converts Anthropic /v1/messages requests to OpenAI chat/completions format,
dispatches through Tusker's passthrough pool, and translates responses back
to Anthropic's wire format (both streaming and non-streaming).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Any, AsyncIterator

from aiohttp import web

from tusker_gateway.budget import BudgetTracker
from tusker_gateway.circuit_breaker import CircuitBreaker, BreakerDecision
from tusker_gateway.errors import BadRequestError
from tusker_gateway.metrics import MetricsRegistry
from tusker_gateway.passthrough import PassthroughClient
from tusker_gateway.pools import PoolManager
from tusker_gateway.quality import QualityDB
from tusker_gateway.rate_limit import RateLimiter
from tusker_gateway.routing import resolve_route
from tusker_gateway.sse import sse_heartbeat_loop
from tusker_gateway.tracing import Tracer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anthropic ↔ OpenAI format translation
# ---------------------------------------------------------------------------


def anthropic_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic Messages API request body to OpenAI chat format.

    Handles:
    - ``system`` (string or list of content blocks) → system message
    - ``messages`` with str or list content → joined text
    - Parameter mapping: ``max_tokens``, ``temperature``, ``top_p``,
      ``stop_sequences`` → ``stop``, ``stream``, ``model``
    """
    openai: dict[str, Any] = {}

    # Model & stream pass through directly.
    if "model" in body:
        openai["model"] = body["model"]
    if "stream" in body:
        openai["stream"] = bool(body["stream"])

    # System prompt → system message prepended to messages list.
    system = body.get("system")
    messages: list[dict[str, Any]] = []
    if system is not None:
        if isinstance(system, str):
            text = system
        elif isinstance(system, list):
            # Anthropic allows list-of-content-blocks: extract text parts.
            parts: list[str] = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            text = "\n".join(parts)
        else:
            text = str(system)
        messages.append({"role": "system", "content": text})

    # Convert Anthropic messages → OpenAI messages.
    for msg in body.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Extract text parts, join with newlines (Anthropic multi-block).
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        parts.append(json.dumps(block))
                elif isinstance(block, str):
                    parts.append(block)
            messages.append({"role": role, "content": "\n".join(parts)})
        elif content is None:
            messages.append({"role": role, "content": ""})
        else:
            messages.append({"role": role, "content": str(content)})

    openai["messages"] = messages

    # Parameter mapping.
    if "max_tokens" in body:
        openai["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        openai["temperature"] = body["temperature"]
    if "top_p" in body:
        openai["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        openai["stop"] = body["stop_sequences"]

    # Tool definitions (Anthropic → OpenAI format).
    if "tools" in body:
        openai["tools"] = _convert_anthropic_tools(body["tools"])

    return openai


def _convert_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool definitions to OpenAI function-calling format."""
    openai_tools: list[dict[str, Any]] = []
    for tool in tools:
        openai_tool: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        }
        openai_tools.append(openai_tool)
    return openai_tools


# Stop-reason mapping (OpenAI → Anthropic).
_STOP_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}


def openai_to_anthropic(result: dict[str, Any], original_model: str) -> dict[str, Any]:
    """Convert a non-streaming OpenAI chat completion response to Anthropic format."""
    message = result.get("choices", [{}])[0].get("message", {})
    content_text = message.get("content") or ""

    # Build Anthropic content blocks.
    content: list[dict[str, Any]] = []
    if content_text:
        content.append({"type": "text", "text": content_text})

    # Convert tool_calls if present.
    for tc in message.get("tool_calls", []):
        func = tc.get("function", {})
        content.append({
            "type": "tool_use",
            "id": f"toolu_{secrets.token_hex(8)}",
            "name": func.get("name", ""),
            "input": json.loads(func.get("arguments", "{}")) if func.get("arguments") else {},
        })

    openai_finish = result.get("choices", [{}])[0].get("finish_reason", "stop")
    stop_reason = _STOP_REASON_MAP.get(openai_finish, "end_turn")

    openai_usage = result.get("usage", {})

    return {
        "id": f"msg_{secrets.token_hex(12)}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": original_model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(openai_usage.get("prompt_tokens", 0)),
            "output_tokens": int(openai_usage.get("completion_tokens", 0)),
        },
    }


# ---------------------------------------------------------------------------
# SSE stream translation (OpenAI → Anthropic)
# ---------------------------------------------------------------------------

class AnthropicSSEStreamTranslator:
    """Wraps an OpenAI SSE byte stream and yields Anthropic SSE events.

    Lifecycle of translated events:
      1. ``message_start``  (once, on first chunk)
      2. ``content_block_start`` (once, on first chunk)
      3. ``content_block_delta`` (per content chunk)
      4. ``content_block_stop`` (once, at end)
      5. ``message_delta`` (once, at end)
      6. ``message_stop`` (once, at end)
    """

    def __init__(
        self,
        openai_stream: AsyncIterator[bytes],
        model: str,
        input_tokens: int = 0,
    ):
        self._stream = openai_stream
        self._model = model
        self._input_tokens = input_tokens
        self._started = False
        self._output_tokens = 0
        self._last_stop_reason = "end_turn"
        self._msg_id = f"msg_{secrets.token_hex(12)}"
        self._emitted_closing = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._emitted_closing:
            raise StopAsyncIteration

        if not self._started:
            self._started = True
            return self._fmt_message_start()

        try:
            raw = await self._stream.__anext__()
        except StopAsyncIteration:
            self._emitted_closing = True
            return self._closing_sequence()

        parsed = self._parse_sse(raw)
        if parsed is None:
            # [DONE] or unparseable — emit closing.
            self._emitted_closing = True
            return self._closing_sequence()

        if parsed.get("choices"):
            delta = parsed["choices"][0].get("delta", {})
            content_text = delta.get("content")
            finish_reason = parsed["choices"][0].get("finish_reason")

            if content_text:
                self._output_tokens += max(1, len(content_text) // 4)
                return self._fmt_text_delta(content_text)

            if finish_reason:
                self._last_stop_reason = _STOP_REASON_MAP.get(finish_reason, "end_turn")

        # Accumulate usage if present.
        usage = parsed.get("usage", {})
        if usage.get("completion_tokens"):
            self._output_tokens = int(usage["completion_tokens"])

        # No content in this chunk but stream continues — skip silently.
        return await self.__anext__()

    def _closing_sequence(self) -> bytes:
        """Build the final three events as a single SSE frame sequence."""
        parts: list[str] = []
        # content_block_stop
        parts.append("event: content_block_stop")
        parts.append(f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}")
        parts.append("")
        # message_delta
        parts.append("event: message_delta")
        parts.append(f'data: {json.dumps({"type": "message_delta", "delta": {"stop_reason": self._last_stop_reason, "stop_sequence": None}, "usage": {"output_tokens": self._output_tokens}})}')
        parts.append("")
        # message_stop
        parts.append("event: message_stop")
        parts.append('data: {"type": "message_stop"}')
        parts.append("")
        return "\n".join(parts).encode("utf-8")

    def _fmt_message_start(self) -> bytes:
        payload = {
            "type": "message_start",
            "message": {
                "id": self._msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self._model,
                "stop_reason": None,
                "usage": {
                    "input_tokens": self._input_tokens,
                    "output_tokens": 0,
                },
            },
        }
        parts = [
            "event: message_start",
            f"data: {json.dumps(payload)}",
            "",
            "event: content_block_start",
            f'data: {json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})}',
            "",
        ]
        return "\n".join(parts).encode("utf-8")

    def _fmt_text_delta(self, text: str) -> bytes:
        payload = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }
        parts = [
            "event: content_block_delta",
            f"data: {json.dumps(payload)}",
            "",
        ]
        return "\n".join(parts).encode("utf-8")

    @staticmethod
    def _parse_sse(raw: bytes) -> dict[str, Any] | None:
        """Extract and parse the JSON data from an OpenAI SSE line."""
        try:
            text = raw.decode("utf-8").strip()
        except Exception:
            return None
        for line in text.split("\n"):
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str == "[DONE]":
                    return None
                try:
                    return json.loads(data_str)
                except json.JSONDecodeError:
                    return None
        return None


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
) -> tuple[str, str, Any]:
    """Dispatch OpenAI-format body through pool fallback.

    If the result is a streaming iterator, wraps it in
    ``AnthropicSSEStreamTranslator`` to produce Anthropic SSE events.
    """
    model = body.get("model", "")
    route = resolve_route(model, body)

    if route.kind in {"pool", "code"} and route.pool_name:
        pool_name = route.pool_name
        excluded: set[tuple[str, str]] = set()
        last_error: Exception | None = None
        pool_mgr = PoolManager(config)
        while True:
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
            prov, mdl = selected
            try:
                result = await client.chat(prov, mdl, body["messages"],
                                          stream=bool(body.get("stream")), tools=tools)
                if breaker is not None:
                    breaker.record_success(prov, mdl)
                if body.get("stream") and hasattr(result, "__aiter__"):
                    result = AnthropicSSEStreamTranslator(
                        result, model=model,
                        input_tokens=_estimated_tokens(body.get("messages", [])),
                    )
                return prov, mdl, result
            except Exception as exc:
                if breaker is not None:
                    breaker.record_failure(prov, mdl)
                last_error = exc
                excluded.add(selected)
    elif route.kind == "passthrough" and route.provider and route.model:
        decision = breaker.check(route.provider, route.model) if breaker else BreakerDecision(allowed=True, state=None)
        if not decision.allowed:
            raise BadRequestError(
                f"circuit open for {route.provider}/{route.model}: {decision.reason}",
                code="circuit_open",
            )
        try:
            result = await client.chat(route.provider, route.model, body["messages"],
                                      stream=bool(body.get("stream")), tools=tools)
            if breaker is not None:
                breaker.record_success(route.provider, route.model)
            if body.get("stream") and hasattr(result, "__aiter__"):
                result = AnthropicSSEStreamTranslator(
                    result, model=model,
                    input_tokens=_estimated_tokens(body.get("messages", [])),
                )
            return route.provider, route.model, result
        except Exception:
            if breaker is not None:
                breaker.record_failure(route.provider, route.model)
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
            openai_body = anthropic_to_openai(body)

            config = request.app["config"]
            client = PassthroughClient(config, QualityDB(config["quality_db_path"]), request.app["http_session"])
            tools = openai_body.pop("tools", None)
            pool_name = _pool_name_for_anthropic(original_model)

            # Rate-limit pre-flight.
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
                        _anthropic_error(rl.reason or "rate limit exceeded", type="rate_limit_error"),
                        status=429,
                        headers=headers,
                    )

            # Budget pre-flight.
            if budget is not None and api_key:
                est = _estimated_tokens(openai_body.get("messages", []))
                decision = budget.check(api_key, pool_name, est)
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
                config, openai_body, client, tools, breaker=breaker,
            )
            logger.debug("selected %s/%s for anthropic model=%s", provider, target_model, original_model)

            # Budget recording.
            if budget is not None and api_key and isinstance(result, dict):
                usage = result.get("usage") or {}
                used = int(usage.get("total_tokens") or _estimated_tokens(openai_body.get("messages", [])))
                budget.record(api_key, pool_name, used)

            # Streaming response.
            if body.get("stream"):
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
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
                    logger.info(
                        "anthropic stream client disconnected provider=%s model=%s err=%s",
                        provider, target_model, exc,
                    )
                except asyncio.CancelledError:
                    stream_ok = False
                    raise
                except Exception as exc:  # noqa: BLE001
                    stream_ok = False
                    logger.warning(
                        "anthropic stream pump failed provider=%s model=%s err=%s",
                        provider, target_model, exc,
                        exc_info=True,
                    )
                finally:
                    stop.set()
                    try:
                        await asyncio.wait_for(hb_task, timeout=hb_interval + 1.0)
                    except asyncio.TimeoutError:
                        hb_task.cancel()
                status = "ok" if stream_ok else status
                _emit(status)
                return resp

            # Non-streaming: convert OpenAI → Anthropic format.
            anthropic_resp = openai_to_anthropic(result, original_model)
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
            )
        except Exception as exc:
            logger.warning("anthropic request failed: %s", exc)
            status = "provider_error"
            _emit(status)
            if budget is not None and api_key and body is not None:
                budget.refund(api_key, pool_name, _estimated_tokens(openai_body.get("messages", [])))
            return web.json_response(
                _anthropic_error(str(exc), type="api_error"),
                status=502,
            )
