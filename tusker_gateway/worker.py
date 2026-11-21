"""Minimal worker mode for the gateway.

When ``TUSKER_WORKER_MODE=1`` (or invoked as ``python -m tusker_gateway.worker``)
the process does not run the full gateway control plane. Instead it exposes a
small HTTP surface that runs process-backed provider adapters (opencode-cli,
claude-code-cli) in this container and streams their OpenAI-compatible output
back to a gateway that delegates to it.

This keeps CLI child processes (each up to ~1 GiB) out of the gateway pod, so
a heavy model can no longer OOM the control plane. Remote HTTP providers stay
in the gateway; only process-backed adapters are expected to run here.
"""
from __future__ import annotations

import json
import os

import aiohttp.web as web

from tusker_gateway.provider_adapters import provider_adapters


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "mode": "worker"})


async def chat(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    provider = str(body.get("provider") or "").strip()
    model = str(body.get("model") or "").strip()
    if not provider or not model:
        return web.json_response({"error": "provider and model required"}, status=400)

    adapter = provider_adapters.get(provider)
    if adapter is None:
        return web.json_response({"error": f"no adapter for {provider}"}, status=404)

    messages = body.get("messages") or []
    stream = bool(body.get("stream", False))
    tools = body.get("tools")
    tool_choice = body.get("tool_choice")

    try:
        result = await adapter.chat(
            provider=provider,
            model=model,
            messages=messages,
            stream=stream,
            tools=tools,
            tool_choice=tool_choice,
        )
    except Exception as exc:  # noqa: BLE001 - surface as HTTP error
        status = getattr(exc, "status", 502)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 502
        return web.json_response(
            {"error": {"message": str(exc), "code": getattr(exc, "code", None)}},
            status=status,
        )

    if hasattr(result, "__aiter__"):
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)
        async for chunk in result:
            data = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
            await resp.write(data)
        await resp.write_eof()
        return resp

    return web.json_response(result)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/worker/chat", chat)
    return app


def main() -> None:
    port = int(os.environ.get("TUSKER_WORKER_PORT", "8643"))
    web.run_app(create_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()