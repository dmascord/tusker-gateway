"""Provider client and passthrough dispatch.

Handles the actual HTTP call to upstream providers, with token rotation
for Codex OAuth and cooldown tracking on failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncIterator

import aiohttp

from tusker_gateway.cooldown import global_tracker
from tusker_gateway.errors import (
    GatewayError,
    ProviderError,
    RateLimitError,
)
from tusker_gateway.quality import QualityDB

logger = logging.getLogger(__name__)


# Per-read timeout for upstream SSE streams. The `total` budget below is the
# hard cap on the whole request; `sock_read` caps the *gap* between bytes so a
# stalled provider surfaces as a clean timeout instead of hanging silently
# until `total` expires (which is what causes the "socket connection was
# closed unexpectedly" symptom on the client).
_UPSTREAM_STREAM_SOCK_READ_SECS = float(
    os.environ.get("TUSKER_UPSTREAM_SOCK_READ_SECS", "90")
)

# Build per-provider endpoints dict from the normalized registry in config.py
# Falls back to legacy hard-coded mapping if the registry is unavailable.
def _init_provider_endpoints() -> dict[str, dict[str, Any]]:
    try:
        from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
        out: dict[str, dict[str, Any]] = {}
        for name, pc in DEFAULT_PROVIDER_REGISTRY.items():
            entry: dict[str, Any] = {
                "base_url": pc.base_url,
                "chat_path": pc.chat_path,
                "auth_type": pc.auth_type or pc.kind,
            }
            if pc.model_header:
                entry["model_header"] = pc.model_header
            out[name] = entry
        if out:
            return out
    except ImportError:
        pass
    return {
        "github-copilot": {"base_url": "https://api.githubcopilot.com", "chat_path": "/chat/completions", "auth_type": "oauth", "model_header": "x-github-gpt-model"},
        "github-copilot-enterprise": {"base_url": "https://api.githubcopilot.com", "chat_path": "/chat/completions", "auth_type": "oauth", "model_header": "x-github-gpt-model"},
        "openai-codex": {"base_url": "https://api.github.com/copilot", "chat_path": "/chat/completions", "auth_type": "oauth", "model_header": "x-openai-gpt-model"},
        "openai": {"base_url": "https://api.openai.com", "chat_path": "/v1/chat/completions", "auth_type": "bearer"},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "chat_path": "/chat/completions", "auth_type": "bearer"},
        "groq": {"base_url": "https://api.groq.com/openai", "chat_path": "/v1/chat/completions", "auth_type": "bearer"},
        "local-llm": {"base_url": "http://localhost:11434", "chat_path": "/v1/chat/completions", "auth_type": "bearer"},
        "zai": {"base_url": "https://api.z.ai/api/paas", "chat_path": "/v4/chat/completions", "auth_type": "bearer"},
    }


PROVIDER_ENDPOINTS: dict[str, dict[str, Any]] = _init_provider_endpoints()


# Provider names whose endpoints use OAuth or Codex-style token-based auth.
# Exposed for test assertions and downstream code that needs to know which
# providers require credential pools.
OAUTH_PROVIDERS: frozenset[str] = frozenset(
    name for name, ep in PROVIDER_ENDPOINTS.items()
    if ep.get("auth_type") in ("oauth", "codex")
)


def _creds_access_token(cred: dict[str, Any]) -> str | None:
    """Return the access token from a credential, handling both formats."""
    return cred.get("access_token") or cred.get("token")


def _creds_refresh_token(cred: dict[str, Any]) -> str | None:
    """Return the refresh token from a credential."""
    return cred.get("refresh_token")


def _creds_expires_at(cred: dict[str, Any]) -> float:
    """Return credential expiry as epoch seconds (handles expires_at & expires_at_ms)."""
    ms = cred.get("expires_at_ms")
    if ms:
        return float(ms) / 1000.0
    return float(cred.get("expires_at", 0) or 0)


class CodexTokenRotator:
    """Rotates Codex OAuth tokens across a credential pool.

    Supports Hermes-format credentials (access_token, refresh_token, expires_at_ms)
    and legacy format (token, refresh_token, expires_at).

    When a token is near expiry, ``get_token()`` automatically attempts a
    refresh via the Copilot token-exchange endpoint and persists the updated
    credential back to the auth file if one is configured.
    """

    _JWT_REFRESH_MARGIN_SECONDS = 120

    def __init__(
        self,
        credentials: list[dict[str, Any]],
        *,
        auth_file: str | None = None,
        http_client: Any | None = None,
    ):
        self._creds: list[dict[str, Any]] = list(credentials)
        self._index = 0
        self._lock = asyncio.Lock()
        self._auth_file = auth_file
        self._http = http_client  # aiohttp.ClientSession for exchange

    @property
    def size(self) -> int:
        return len(self._creds)

    def reload(self, credentials: list[dict[str, Any]]) -> None:
        """Reload the pool from external source (e.g. file)."""
        self._creds = list(credentials)
        self._index = min(self._index, max(len(self._creds) - 1, 0))

    async def get_token(self) -> str | None:
        """Return the current active token, or None if no credentials.

        If the token is near expiry, attempts an automatic refresh.
        """
        if not self._creds:
            return None
        async with self._lock:
            idx = self._index % len(self._creds)
            cred = self._creds[idx]
            label = cred.get("label", cred.get("id", f"cred#{idx}"))
            logger.debug("codex rotator: index=%d/%d label=%s", idx, len(self._creds), label)
            if self._http and self._is_near_expiry(cred):
                try:
                    cred = await self._refresh_one(cred)
                    self._creds[self._index % len(self._creds)] = cred
                    self._persist()
                except Exception:
                    pass  # best-effort refresh
            return _creds_access_token(cred)

    async def advance(self) -> None:
        """Move to the next credential in the pool."""
        if len(self._creds) > 1:
            async with self._lock:
                self._index = (self._index + 1) % len(self._creds)

    async def refresh_if_needed(self, cred: dict[str, Any]) -> dict[str, Any]:
        """Check token expiry and refresh if needed."""
        if self._is_near_expiry(cred):
            try:
                return await self._refresh_one(cred)
            except Exception:
                return cred
        return cred

    async def _refresh_one(self, cred: dict[str, Any]) -> dict[str, Any]:
        """Exchange the raw (refresh) token for a new API token."""
        from tusker_gateway.copilot_exchange import exchange_copilot_token

        refresh = _creds_refresh_token(cred)
        if not refresh:
            return cred

        # Use the credential's host (GHE) if set
        base_url = None
        host = cred.get("host")
        if host and host not in ("github.com",):
            base_url = f"https://{host}/copilot"

        try:
            token, expires_at = await exchange_copilot_token(refresh, base_url=base_url, http=self._http)
            cred["access_token"] = token
            cred["expires_at_ms"] = int(expires_at * 1000)
            return cred
        except ValueError:
            return cred

    @classmethod
    def _is_near_expiry(cls, cred: dict[str, Any]) -> bool:
        expires_at = _creds_expires_at(cred)
        return bool(expires_at and time.time() >= expires_at - cls._JWT_REFRESH_MARGIN_SECONDS)

    def _persist(self) -> None:
        """Write the current pool back to the auth file (Hermes format)."""
        if not self._auth_file:
            return
        try:
            from tusker_gateway.copilot_enroll import save_auth_file
            save_auth_file(self._creds, self._auth_file)
        except Exception:
            pass  # best-effort persistence


class PassthroughClient:
    """HTTP client for provider passthrough requests."""

    def __init__(
        self,
        config: dict[str, Any],
        quality_db: QualityDB,
        http_client: aiohttp.ClientSession,
    ):
        self._config = config
        self._quality = quality_db
        self._http = http_client
        auth_file = config.get("auth_file")
        if not auth_file:
            import os
            auth_file = os.getenv("TUSKER_AUTH_FILE")
        if not auth_file:
            from pathlib import Path
            auth_file = str(Path.home() / ".hermes" / "auth.json")
        codex_creds = config.get("codex_credentials", [])
        if not codex_creds:
            try:
                from tusker_gateway.copilot_enroll import load_auth_file
                codex_creds = load_auth_file(auth_file)
            except Exception:
                codex_creds = []
        self._codex_rotator = CodexTokenRotator(codex_creds, auth_file=auth_file, http_client=http_client)

    async def chat(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        *,
        stream: bool = False,
        api_key: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        upstream_gateway: str | None = None,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        """Make a passthrough chat completions call to the provider."""
        logger.info('passthrough %s/%s stream=%s', provider, model, stream)

        # Resolve the endpoint up front so the dispatch can decide between
        # the standard chat-completions passthrough and the openai-codex
        # Responses API adapter.
        if upstream_gateway:
            endpoint = {"base_url": upstream_gateway.rstrip("/"), "chat_path": "/v1/chat/completions", "auth_type": "bearer"}
        else:
            endpoint = PROVIDER_ENDPOINTS.get(provider)
            if not endpoint:
                raise ProviderError(f"Unknown provider: {provider}")

        # openai-codex uses the Responses API only when its endpoint is actually
        # configured for it (chat_path ends with "/responses"). Test patches
        # sometimes redirect it to a regular /chat/completions endpoint; in
        # that case we treat it as a normal bearer passthrough.
        if provider == "openai-codex" and endpoint.get("chat_path", "").endswith("/responses"):
            return await self._chat_codex(
                model, messages,
                stream=stream, api_key=api_key, tools=tools,
                extra_headers=extra_headers, extra_body=extra_body,
            )

        base_url = endpoint["base_url"]
        path = endpoint["chat_path"]
        url = f"{base_url}{path}"
        headers, body = await self._build_request(
            provider, model, messages,
            stream=stream, api_key=(self._config["api_keys"][0] if upstream_gateway else api_key),
            tools=tools,
            extra_headers=extra_headers, extra_body=extra_body,
            endpoint=endpoint,
        )

        start = time.monotonic()
        if stream:
            resp = await self._http.request(
                "POST", url, headers=headers, json=body,
                timeout=aiohttp.ClientTimeout(
                    total=120,
                    # Cap the *gap* between SSE bytes. If the provider goes
                    # silent for this long, aiohttp raises a clean asyncio
                    # TimeoutError instead of letting the socket hang until
                    # the 120s `total` fires. The error then surfaces to the
                    # caller as a normal exception (which the endpoint layer
                    # already logs + refunds) — never as the opaque
                    # "socket connection was closed unexpectedly" that
                    # appears when the underlying TCP gets reaped first.
                    sock_read=_UPSTREAM_STREAM_SOCK_READ_SECS,
                ),
            )
            try:
                await self._check_response(resp)
            except RateLimitError as exc:
                from tusker_gateway.cooldown import _cooldown_seconds_for_429
                tracker = global_tracker()
                body_text = exc.body or "429 rate limit"
                seconds = _cooldown_seconds_for_429({"body": body_text, "headers": dict(resp.headers)})
                logger.warning('429 from %s/%s, cooldown=%.0fs', provider, model, seconds)
                tracker.cooldown(provider, model, seconds)
                try:
                    from tusker_gateway.persistent_cooldown import PersistentCooldownStore
                    from pathlib import Path
                    db_path = Path(self._config.get("quality_db_path", "data/quality.db")).parent / "cooldowns.db"
                    store = PersistentCooldownStore(db_path=db_path)
                    store.record(provider, model, seconds)
                except Exception:
                    pass
                raise
            except Exception:
                resp.release()
                raise
            # Record quality on streaming success
            try:
                latency_ms = (time.monotonic() - start) * 1000
                await self._record_quality(provider, model, True, latency_ms)
                global_tracker().clear_failures(provider)
            except Exception:
                pass
            return self._stream_events(resp, provider=provider, model=model)
        try:
            async with self._http.request(
                "POST", url, headers=headers, json=body,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                await self._check_response(resp)
                result = await resp.json()
                from tusker_gateway.tool_formats import normalize_response_tool_calls
                result = normalize_response_tool_calls(result)
                latency_ms = (time.monotonic() - start) * 1000
                await self._record_quality(provider, model, True, latency_ms)
                logger.debug('quality recorded %s/%s success=True', provider, model)
                global_tracker().clear_failures(provider)
                return result
        except RateLimitError as exc:
            from tusker_gateway.cooldown import _cooldown_seconds_for_429
            tracker = global_tracker()
            body_text = exc.body or "429 rate limit"
            seconds = _cooldown_seconds_for_429({"body": body_text, "headers": exc.headers})
            logger.warning('429 from %s/%s, cooldown=%.0fs', provider, model, seconds)
            tracker.cooldown(provider, model, seconds)
            try:
                from tusker_gateway.persistent_cooldown import PersistentCooldownStore
                from pathlib import Path
                db_path = Path(self._config.get("quality_db_path", "data/quality.db")).parent / "cooldowns.db"
                store = PersistentCooldownStore(db_path=db_path)
                store.record(provider, model, seconds)
            except Exception:
                pass
            raise
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            await self._record_quality(provider, model, False, latency_ms)
            logger.debug('quality recorded %s/%s success=False', provider, model)
            tracker = global_tracker()
            if tracker.record_failure(provider):
                tracker.cooldown(provider, "", 300.0) # Provider-level cooldown
                try:
                    from tusker_gateway.persistent_cooldown import PersistentCooldownStore
                    from pathlib import Path
                    db_path = Path(self._config.get("quality_db_path", "data/quality.db")).parent / "cooldowns.db"
                    store = PersistentCooldownStore(db_path=db_path)
                    store.record_provider(provider, 300.0)
                except Exception:
                    pass
            logger.warning('provider error %s/%s: %s', provider, model, exc)
            raise ProviderError(str(exc)) from exc
    async def _build_request(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        *,
        stream: bool,
        api_key: str | None,
        tools: list[dict[str, Any]] | None = None,
        extra_headers: dict[str, str] | None,
        extra_body: dict[str, Any] | None,
        endpoint: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        from tusker_gateway.auth_strategies import get_auth_strategy
        from tusker_gateway.models import ProviderConfig
        from tusker_gateway.tool_formats import normalize_tools

        endpoint_model = ProviderConfig.from_raw(endpoint)
        strategy = get_auth_strategy(endpoint_model.auth_type, self._codex_rotator)
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            **(extra_headers or {}),
        }
        headers.update(
            await strategy.headers(
                self._config, provider, model, api_key, endpoint_model
            )
        )
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if tools:
            body["tools"] = normalize_tools(tools)
        if extra_body:
            body.update(extra_body)
        return headers, body
    async def _chat_codex(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        stream: bool,
        api_key: str | None,
        tools: list[dict[str, Any]] | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        from tusker_gateway.auth_strategies import get_auth_strategy
        from tusker_gateway.models import ProviderConfig
        from tusker_gateway.tool_formats import normalize_tools
        endpoint_raw = PROVIDER_ENDPOINTS["openai-codex"]
        endpoint_model = ProviderConfig.from_raw(endpoint_raw)
        # Honor the patched endpoint's auth_type (test-only override path).
        # Default to "codex" so production keeps its dedicated auth strategy.
        auth_type = endpoint_raw.get("auth_type") or endpoint_model.auth_type or "codex"
        if auth_type == "codex":
            strategy = get_auth_strategy("codex", self._codex_rotator)
        elif auth_type == "oauth":
            strategy = get_auth_strategy("oauth", self._codex_rotator)
        else:
            strategy = get_auth_strategy("bearer", self._codex_rotator)
        headers = {
            "Content-Type": "application/json",
            **(extra_headers or {}),
            **await strategy.headers(self._config, "openai-codex", model, api_key, endpoint_model),
        }
        input_data: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role == "system":
                input_data.append({"role": "system", "content": [{"type": "input_text", "text": content}]})
            elif role == "user":
                input_data.append({"role": "user", "content": [{"type": "input_text", "text": content}]})
            elif role == "assistant":
                # Responses API expects plain content for assistant *input*
                # items (output_text is for output items only). Hermes-agent
                # and the Codex CLI both send plain strings here.
                input_data.append({"role": "assistant", "content": str(content) if content is not None else ""})
        # Codex backend requires stream=true; force it here regardless of
        # what the caller asked for (the response parser handles SSE).
        body: dict[str, Any] = {
            "model": model,
            "input": input_data,
            "stream": True,
            "store": False,
        }
        # Pull the first system message out into `instructions` if present.
        # Falls back to None so OpenAI can apply its own default.
        if messages and messages[0].get("role") == "system":
            sys_text = str(messages[0].get("content") or "").strip()
            if sys_text:
                body["instructions"] = sys_text
        # Default reasoning effort: medium. Codex backend uses native chain-
        # of-thought; without this the backend may default to a non-thinking
        # variant or reject the request entirely.
        body["reasoning"] = {"effort": "medium", "summary": "auto"}
        if tools:
            body["tools"] = [
                {"type": "function", "name": t["function"]["name"],
                 "description": t["function"].get("description", ""),
                 "parameters": t["function"].get("parameters", {"type": "object", "properties": {}})}
                for t in normalize_tools(tools)
            ]
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = True
        if extra_body:
            # The codex Responses API uses `max_output_tokens`, not the
            # chat-completions `max_tokens` / `max_completion_tokens`. Map
            # them so the provider doesn't reject the request with
            # "Unsupported parameter: max_completion_tokens".
            mapped = dict(extra_body)
            if "max_output_tokens" not in mapped:
                mapped["max_output_tokens"] = mapped.pop("max_tokens", mapped.pop("max_completion_tokens", None))
            # Drop chat-completions-only params the Responses API rejects.
            for k in ("max_tokens", "max_completion_tokens"):
                mapped.pop(k, None)
            body.update(mapped)
        url = f"{endpoint_raw['base_url']}{endpoint_raw['chat_path']}"
        start = time.monotonic()
        resp = await self._http.request("POST", url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=120))
        try:
            await self._check_response(resp)
        except RateLimitError as exc:
            resp.release()
            from tusker_gateway.cooldown import _cooldown_seconds_for_429
            tracker = global_tracker()
            seconds = _cooldown_seconds_for_429({"body": (exc.body or "429"), "headers": {}})
            tracker.cooldown("openai-codex", model, seconds)
            try:
                from tusker_gateway.persistent_cooldown import PersistentCooldownStore
                from pathlib import Path
                db_path = Path(self._config.get("quality_db_path", "data/quality.db")).parent / "cooldowns.db"
                PersistentCooldownStore(db_path=db_path).record("openai-codex", model, seconds)
            except Exception:
                pass
            # Advance to next credential so the next request doesn't keep
            # hammering a rate-limited account.
            if self._codex_rotator and self._codex_rotator.size > 1:
                await self._codex_rotator.advance()
            raise
        except Exception as exc:
            # Read & log the body before releasing the response so transient
            # upstream errors (model unsupported, quota, etc.) show up in logs.
            try:
                err_body = await resp.text()
            except Exception:
                err_body = "<could not read body>"
            logger.warning(
                "codex error %s stream=%s: %s",
                model, stream, err_body[:300],
            )
            resp.release()
            # Advance to next credential on any non-2xx so a sick account
            # doesn't cause the breaker to trip after 5 consecutive failures.
            if self._codex_rotator and self._codex_rotator.size > 1:
                await self._codex_rotator.advance()
            raise
        result = await self._parse_codex_sse_async(resp)
        latency_ms = (time.monotonic() - start) * 1000
        await self._record_quality("openai-codex", model, True, latency_ms)
        return result
    async def _parse_codex_sse_async(self, resp: aiohttp.ClientResponse) -> dict[str, Any]:
        """Iterate a Codex SSE response and assemble an OpenAI-compatible dict."""
        content_parts: list[str] = []
        tool_calls: dict[str, dict[str, Any]] = {}
        tool_order: list[str] = []
        usage_obj: dict[str, Any] | None = None
        async for raw_line in resp.content:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                evt = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            etype = evt.get("type", "")
            if etype == "response.output_text.delta":
                delta = evt.get("delta") or ""
                if delta:
                    content_parts.append(delta)
            elif etype == "response.output_item.added":
                item = evt.get("item") or {}
                if item.get("type") == "function_call":
                    call_id = str(item.get("call_id") or f"call_{len(tool_order) + 1}")
                    tool_calls[call_id] = {"id": call_id, "type": "function", "function": {"name": item.get("name", ""), "arguments": ""}}
                    tool_order.append(call_id)
            elif etype == "response.function_call_arguments.delta":
                call_id = str(evt.get("call_id") or "")
                if call_id in tool_calls:
                    tool_calls[call_id]["function"]["arguments"] += evt.get("delta") or ""
            elif etype == "response.completed":
                response = evt.get("response") or {}
                usage_obj = response.get("usage")
            elif etype == "response.failed":
                err = evt.get("error") or {}
                raise ProviderError(f"Codex response failed: {err.get('code','unknown')} {err.get('message','')[:200]}")
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if tool_order:
            message["tool_calls"] = [tool_calls[cid] for cid in tool_order]
        return {
            "id": f"chatcmpl-codex-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "openai-codex",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_order else "stop"}],
            "usage": usage_obj or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


    @staticmethod
    async def _check_response(resp: aiohttp.ClientResponse) -> None:
        if resp.status == 200:
            return
        logger.warning('provider returned %d', resp.status)
        body = await resp.text()
        if resp.status == 401:
            raise ProviderError("Provider authentication failed", code="auth_error")
        if resp.status == 403:
            raise ProviderError("Provider access forbidden", code="forbidden")
        if resp.status == 429:
            raise RateLimitError(body=body, headers=dict(resp.headers))
        if resp.status >= 500:
            raise ProviderError(f"Provider returned {resp.status}: {body[:200]}", code="provider_error")
        if resp.status != 200:
            raise ProviderError(f"Provider returned {resp.status}: {body[:200]}", code="provider_error")

    async def _stream_events(
        self,
        resp: aiohttp.ClientResponse,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Pump an upstream SSE response byte-for-byte to the gateway caller.

        `aiohttp.ClientConnectionError` historically meant "the gateway's
        caller (aiohttp client connecting to us) went away", so it was
        silent. With this gateway now sitting behind Traefik+Cloudflare and
        the upstream provider behind their own stack, *upstream-side*
        disconnects also surface as connection errors. We log both cases
        at INFO level with the upstream URL + status (when available) so
        operators can correlate with what the caller saw (Bun's opaque
        "socket connection was closed unexpectedly" on the OMP side, or a
        502 from the load balancer).
        """
        upstream = getattr(resp, "url", None)
        upstream_str = str(upstream) if upstream is not None else (provider or "?")
        status = getattr(resp, "status", None)
        try:
            async for chunk in resp.content.iter_any():
                yield chunk
        except aiohttp.ServerDisconnectedError as exc:
            logger.info(
                "upstream server disconnected mid-stream provider=%s model=%s "
                "url=%s status=%s err=%s",
                provider, model, upstream_str, status, exc,
            )
        except aiohttp.ClientConnectionError as exc:
            logger.info(
                "upstream connection error mid-stream provider=%s model=%s "
                "url=%s status=%s err=%s",
                provider, model, upstream_str, status, exc,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "upstream SSE read timeout (>%ss idle) provider=%s model=%s "
                "url=%s status=%s",
                _UPSTREAM_STREAM_SOCK_READ_SECS, provider, model, upstream_str, status,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "upstream SSE read failed provider=%s model=%s "
                "url=%s status=%s err=%s",
                provider, model, upstream_str, status, exc,
                exc_info=True,
            )
        finally:
            resp.release()

    async def _record_quality(
        self, provider: str, model: str, success: bool, latency_ms: float
    ) -> None:
        try:
            if hasattr(self._quality, "record"):
                await self._quality.record(provider, model, success, latency_ms)
        except Exception:
            pass  # Quality DB is best-effort
