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

from tusker_gateway.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderModelRetiredError,
    ProviderRouteDisabledError,
    RateLimitError,
)


def _worker_error(status: int, body: str) -> Exception:
    """Rebuild the adapter's error class from the worker's JSON error body.

    The worker forwards ``code`` / ``upstream_status`` and retry metadata so
    the gateway keeps classifying CLI failures after process isolation. A
    generic ``worker_error`` loses the permanent-failure mark for a retired
    model and the quota cooldown for an exhausted key, leaving dead
    auto-discovered routes in rotation (req_6cf0e5a0d1d0bbd7).
    """
    message = f"worker error {status}: {body[:200]}"
    code = "worker_error"
    upstream_status: int | None = status
    retry_body: str | None = None
    headers: dict[str, str] = {}
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            if isinstance(error.get("code"), str) and error["code"]:
                code = error["code"]
            value = error.get("upstream_status")
            if isinstance(value, int) and not isinstance(value, bool):
                upstream_status = value
            if isinstance(error.get("message"), str) and error["message"]:
                message = error["message"]
            if isinstance(error.get("body"), str):
                retry_body = error["body"]
            raw_headers = error.get("headers")
            if isinstance(raw_headers, dict):
                headers = {str(k): str(v) for k, v in raw_headers.items()}
    except (TypeError, ValueError):
        pass

    if code == "rate_limit_exceeded":
        return RateLimitError(
            message, code=code, body=retry_body or body, headers=headers
        )
    if code == "model_retired":
        return ProviderModelRetiredError(message, code=code)
    if code == "provider_auth_failed":
        return ProviderAuthError(message, code=code)
    exc = ProviderError(message, code=code)
    exc.upstream_status = upstream_status
    exc.upstream_body = retry_body or body
    return exc


class RemoteCLIAdapter:
    """Adapter proxying CLI chat completions to a worker service."""

    def __init__(self, provider: str, worker_url: str | None = None) -> None:
        self._provider = provider
        self._worker_url = (worker_url or "").strip().rstrip("/")

    def _url(self) -> str:
        if self._worker_url:
            return self._worker_url
        import os

        # Backwards-compatible fallbacks for a single shared worker.
        for env in (
            "TUSKER_OPENCODE_WORKER_URL" if self._provider == "opencode-cli" else "TUSKER_CLAUDE_WORKER_URL",
            "TUSKER_CLI_WORKER_URL",
        ):
            value = (os.environ.get(env) or "").strip().rstrip("/")
            if value:
                return value
        return ""

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
        url = self._url()
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
            raise _worker_error(resp.status, body)

        if stream:
            async def _stream() -> AsyncIterator[str]:
                try:
                    async for line in resp.content:
                        if line:
                            # aiohttp yields bytes per line, and the gateway's
                            # stream loop / response capture operate on bytes.
                            # Decoding here made every CLI stream raise a
                            # TypeError once turn capture was enabled.
                            yield line if isinstance(line, bytes) else line.encode("utf-8", errors="replace")
                except Exception as exc:  # noqa: BLE001
                    # Worker connection dropped mid-stream; surface as a
                    # ProviderError so the gateway can render a verdict.
                    raise ProviderError(
                        f"worker stream interrupted: {exc}",
                        code="worker_stream_interrupted",
                    )
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