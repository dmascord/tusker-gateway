"""Isolated HTTP worker for Kilo CLI requests; intended for a dedicated K8s node."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from tusker_gateway.errors import BadRequestError, ProviderError, ProviderRouteDisabledError
from tusker_gateway.provider_adapters.kilo_cli import _WARM_TOOL_INDEX_LIMIT, KiloCLIAdapter

logger = logging.getLogger("tusker_gateway.kilo_worker")
# Text requests never advertise tools, so they use a server without an MCP bridge.
_WARM_SERVER: asyncio.subprocess.Process | None = None
_WARM_SERVER_URL = "http://127.0.0.1:4096"
# Tool requests share one bridge that resolves each request's tools via a pointer.
_WARM_TOOL_SERVER: asyncio.subprocess.Process | None = None
_WARM_TOOL_SERVER_URL = "http://127.0.0.1:4097"
_WARM_SERVER_LOCK = asyncio.Lock()
_BROKER_DIR = "/tmp/tusker-kilo-broker"
_ALLOWED_MODELS = frozenset({
    "groq/openai/gpt-oss-20b",
    "groq/qwen/qwen3.8-27b",
    "kilo/kilo-auto/free",
})
_CONCURRENCY = asyncio.Semaphore(2)


def _broker_config() -> dict[str, Any]:
    """Config for the shared MCP bridge plus the tool ids the worker may enable.

    Request tool manifests use index-based MCP names (``gateway_tool_N``), so the
    warm server pre-authorizes that bounded name space instead of per-request
    names. Requests exceeding the bound fall back to the isolated CLI path.
    """
    permission: dict[str, str] = {"*": "deny"}
    for index in range(_WARM_TOOL_INDEX_LIMIT):
        permission[f"gateway_gateway_tool_{index}"] = "allow"
    return {
        "permission": permission,
        "plugin": [],
        "mcp": {
            "gateway": {
                "type": "local",
                "command": [sys.executable, "-m", "tusker_gateway.provider_adapters.mcp_stdio"],
                "environment": {"TUSKER_MCP_BROKER_DIR": _BROKER_DIR},
                "timeout": 120000,
            },
        },
    }


async def _ensure_warm_server() -> str:
    global _WARM_SERVER
    async with _WARM_SERVER_LOCK:
        if _WARM_SERVER is not None and _WARM_SERVER.returncode is None:
            return _WARM_SERVER_URL
        started = asyncio.get_running_loop().time()
        _WARM_SERVER = await asyncio.create_subprocess_exec(
            "kilo", "serve", "--pure", "--hostname", "127.0.0.1",
            "--port", _WARM_SERVER_URL.rsplit(":", 1)[1],
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await asyncio.sleep(0.2)
        logger.info("kilo warm server started pid=%s startup_s=%.3f", _WARM_SERVER.pid, asyncio.get_running_loop().time() - started)
        return _WARM_SERVER_URL


async def _ensure_warm_tool_server() -> str:
    """Start (or reuse) the warm server that serves tool requests through the bridge."""
    global _WARM_TOOL_SERVER
    async with _WARM_SERVER_LOCK:
        if _WARM_TOOL_SERVER is not None and _WARM_TOOL_SERVER.returncode is None:
            return _WARM_TOOL_SERVER_URL
        broker_dir = Path(_BROKER_DIR)
        work_dir = broker_dir / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["KILO_CONFIG_CONTENT"] = json.dumps(_broker_config(), separators=(",", ":"))
        env["KILO_DISABLE_PROJECT_CONFIG"] = "true"
        started = asyncio.get_running_loop().time()
        _WARM_TOOL_SERVER = await asyncio.create_subprocess_exec(
            "kilo", "serve", "--pure", "--hostname", "127.0.0.1",
            "--port", _WARM_TOOL_SERVER_URL.rsplit(":", 1)[1],
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
            cwd=str(work_dir),
        )
        await asyncio.sleep(0.2)
        logger.info(
            "kilo warm tool server started pid=%s startup_s=%.3f broker_dir=%s tool_limit=%d",
            _WARM_TOOL_SERVER.pid,
            asyncio.get_running_loop().time() - started,
            broker_dir,
            _WARM_TOOL_INDEX_LIMIT,
        )
        return _WARM_TOOL_SERVER_URL


async def _stop_warm_servers(_app: web.Application) -> None:
    global _WARM_SERVER, _WARM_TOOL_SERVER
    for name, process in (("text", _WARM_SERVER), ("tools", _WARM_TOOL_SERVER)):
        if process is None or process.returncode is not None:
            continue
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        logger.info("kilo warm %s server stopped pid=%s", name, process.pid)
    _WARM_SERVER = None
    _WARM_TOOL_SERVER = None


def _warm_enabled() -> bool:
    return os.environ.get("TUSKER_KILO_WARM_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


async def _start_warm_servers(_app: web.Application) -> None:
    """Best-effort warm server startup; every failure degrades to the isolated CLI."""
    if not _warm_enabled():
        logger.info("kilo warm servers disabled by TUSKER_KILO_WARM_ENABLED")
        return
    try:
        os.environ["TUSKER_KILO_WARM_SERVER_URL"] = await _ensure_warm_server()
    except Exception as exc:
        logger.warning("kilo warm server unavailable (%s); text uses the isolated CLI", type(exc).__name__)
        os.environ.pop("TUSKER_KILO_WARM_SERVER_URL", None)
    broker_dir = Path(_BROKER_DIR)
    try:
        broker_dir.mkdir(parents=True, exist_ok=True)
        os.environ["TUSKER_MCP_BROKER_DIR"] = str(broker_dir)
        os.environ["TUSKER_KILO_WARM_TOOL_SERVER_URL"] = await _ensure_warm_tool_server()
    except Exception as exc:  # tool requests then fall back to the isolated CLI path
        logger.warning("kilo warm tool server unavailable (%s); tools use the isolated CLI", type(exc).__name__)
        os.environ.pop("TUSKER_KILO_WARM_TOOL_SERVER_URL", None)
        os.environ.pop("TUSKER_MCP_BROKER_DIR", None)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "worker": "kilo",
        "warm_text": _WARM_SERVER is not None and _WARM_SERVER.returncode is None,
        "warm_tools": _WARM_TOOL_SERVER is not None and _WARM_TOOL_SERVER.returncode is None,
    })


async def chat(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (ValueError, web.HTTPException):
        raise web.HTTPBadRequest(text="invalid JSON request")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be an object")
    model = body.get("model")
    messages = body.get("messages")
    if model not in _ALLOWED_MODELS:
        return web.json_response(
            {"error": {"code": "unsupported_model", "message": "Kilo model is not enabled on this worker"}},
            status=400,
        )
    if not isinstance(messages, list) or not messages or not all(isinstance(item, dict) for item in messages):
        raise web.HTTPBadRequest(text="messages must be a non-empty array of objects")

    adapter = KiloCLIAdapter()
    async with _CONCURRENCY:
        try:
            response_model = body.get("public_model")
            if not isinstance(response_model, str) or not response_model.startswith("kilo-cli/"):
                response_model = model
            result: Any = await adapter.chat(
                provider="kilo-cli",
                model=response_model,
                messages=messages,
                stream=body.get("stream") is True,
                tools=body.get("tools"),
                tool_choice=body.get("tool_choice"),
            )
        except (BadRequestError, ProviderError, ProviderRouteDisabledError) as exc:
            status = 503 if isinstance(exc, (ProviderError, ProviderRouteDisabledError)) else 400
            return web.json_response(
                {"error": {"code": getattr(exc, "code", "kilo_worker_failed"), "message": str(exc)}},
                status=status,
            )
        except Exception as exc:  # keep worker failures private; log class, not request content
            logger.exception("unexpected Kilo worker failure: %s", type(exc).__name__)
            return web.json_response(
                {"error": {"code": "kilo_worker_failed", "message": "Kilo worker request failed"}},
                status=502,
            )
        if hasattr(result, "__aiter__"):
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            )
            await response.prepare(request)
            try:
                async for frame in result:
                    await response.write(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Kilo stream failed: %s", getattr(exc, "code", type(exc).__name__))
                error = {
                    "error": {
                        "message": str(exc) or "Kilo stream failed",
                        "type": "provider_error",
                        "code": getattr(exc, "code", "kilo_worker_failed"),
                    },
                }
                await response.write(f"data: {json.dumps(error)}\n\ndata: [DONE]\n\n".encode())
            return response
    return web.json_response(result)


def create_app() -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.on_startup.append(_start_warm_servers)
    app.on_cleanup.append(_stop_warm_servers)
    app.router.add_get("/healthz", health)
    app.router.add_post("/v1/chat/completions", chat)
    return app


def main() -> None:
    os.environ.setdefault("TUSKER_KILO_CLI_ENABLED", "true")
    web.run_app(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8643")))


if __name__ == "__main__":
    main()