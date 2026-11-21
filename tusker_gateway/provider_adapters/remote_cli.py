"""Remote adapter that delegates process-backed CLI providers to a worker pod.

When ``TUSKER_CLI_WORKER_URL`` is set, the gateway no longer runs opencode-cli
or claude-code-cli child processes in its own container. Instead it forwards
the request to the worker service, which runs the adapters in a separate pod.
This keeps CLI memory (up to ~1 GiB per invocation) out of the gateway pod and
prevents a heavy model from OOM-killing the control plane.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from tusker_gateway.errors import ProviderError, ProviderRouteDisabledError


def _worker_url() -> str:
    return (os.environ.get("TUSKER_CLI_WORKER_URL") or "").strip().rstrip("/")


class RemoteCLIAdapter:
    """Adapter proxying CLI chat completions to a worker service."""

    def __init__(self, provider: str) -> None:
        self._provider = provider

    async def chat(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> dict[str, Any] | AsyncIterator[str]:
        url = _worker_url()
        if not url:
            raise ProviderRouteDisabledError(provider)

        payload = {
            "provider": provider,
            "model": model,
            "messages": messages,
            "stream": stream,
            "tools": tools,
            "tool_choice": tool_choice,
        }

        timeout = aiohttp.ClientTimeout(total=600)
        session = aiohttp.ClientSession(timeout=timeout)
        try:
            resp = await session.post(f"{url}/worker/chat", json=payload)
        except Exception as exc:  # noqa: BLE001
            await session.close()
            raise ProviderError(f"worker unreachable: {exc}", code="worker_unavailable")

        if resp.status >= 400:
            try:
                body = await resp.text()
            except Exception:  # noqa: BLE001
                body = ""
            await resp.release()
            await session.close()
            raise ProviderError(
                f"worker error {resp.status}: {body[:200]}",
                code="worker_error",
            )

        if stream:
            async def _stream() -> AsyncIterator[str]:
                try:
                    async for line in resp.content:
                        if line:
                            yield line.decode("utf-8", errors="replace")
                finally:
                    await resp.release()
                    await session.close()

            return _stream()

        try:
            data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            await resp.release()
            await session.close()
            raise ProviderError(f"worker bad response: {exc}", code="worker_error")
        await resp.release()
        await session.close()
        return data