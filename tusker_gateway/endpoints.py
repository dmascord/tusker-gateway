"""Endpoint handlers: /models, /chat/completions, /responses."""
from __future__ import annotations

import time
import uuid
from typing import Any

from aiohttp import web

from tusker_gateway.errors import BadRequestError, openai_error
from tusker_gateway.passthrough import PassthroughClient
from tusker_gateway.pools import PoolManager
from tusker_gateway.quality import QualityDB
from tusker_gateway.routing import resolve_route
from tusker_gateway.sse import sse_done


def _pool_name(body: dict[str, Any]) -> str | None:
    route = resolve_route(body.get("model"), body)
    return route.pool_name or "code" if route.kind in {"pool", "code"} else None


async def _call_with_pool_fallback(
    config: dict[str, Any],
    body: dict[str, Any],
    client: PassthroughClient,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[str, str, Any]:
    """Call a pool candidate, trying the next candidate after provider failure."""
    pool_name = _pool_name(body)
    if pool_name is None:
        provider, model = _route_target(config, body)
        return provider, model, await client.chat(provider, model, body["messages"], stream=bool(body.get("stream")), tools=tools)

    excluded: set[tuple[str, str]] = set()
    last_error: Exception | None = None
    while True:
        selected = PoolManager(config).select(pool_name, excluded=excluded)
        if not selected:
            if last_error is not None:
                raise last_error
            raise BadRequestError("No healthy models in pool", code="no_healthy_models")
        provider, model = selected
        try:
            result = await client.chat(provider, model, body["messages"], stream=bool(body.get("stream")), tools=tools)
            return provider, model, result
        except Exception as exc:
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


async def chat_completions_handler(request: web.Request) -> web.Response | web.StreamResponse:
    """POST /v1/chat/completions."""
    try:
        body = _validate_chat_body(await request.json())
        config = request.app["config"]
        client = PassthroughClient(config, QualityDB(config["quality_db_path"]), request.app["http_session"])
        tools = body.get("tools") if isinstance(body.get("tools"), list) else None
        provider, target_model, result = await _call_with_pool_fallback(config, body, client, tools)
        if body.get("stream", False):
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"})
            await resp.prepare(request)
            async for chunk in result:
                await resp.write(chunk)
            await resp.write(sse_done())
            return resp
        return web.json_response(result)
    except BadRequestError as exc:
        return web.json_response(openai_error(exc.message, code=exc.code, error_type=exc.error_type), status=exc.status)
    except Exception as exc:
        return web.json_response(openai_error(str(exc), code="provider_error", error_type="provider_error"), status=502)


async def responses_handler(request: web.Request) -> web.Response | web.StreamResponse:
    try:
        body = await request.json()
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
