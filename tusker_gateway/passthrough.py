"""Provider client and passthrough dispatch.

Handles the actual HTTP call to upstream providers, with token rotation
for Codex OAuth and cooldown tracking on failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, AsyncIterator

import aiohttp

from tusker_gateway.cooldown import global_tracker
from tusker_gateway.errors import (
    GatewayError,
    ProviderCapacityError,
    ProviderError,
    RateLimitError,
)
from tusker_gateway.provider_usage import (
    CapacityLease,
    capacity_busy_cooldown_seconds,
    capacity_controller,
    capacity_cooldown_seconds,
    capacity_group_for_route,
    default_provider_usage_db_path,
    is_capacity_error,
    ProviderUsageDB,
)
from tusker_gateway.quality import QualityDB

logger = logging.getLogger(__name__)

_SENSITIVE_ERROR_VALUE_RE = re.compile(
    r"(?i)(\b(?:authorization|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token)\b\s*[:=]\s*)([\"']?)[^\s,\"'}]+",
)


def _safe_upstream_body(body: str | None, *, limit: int = 500) -> str:
    """Return a bounded provider error body without echoing credentials."""
    if not body:
        return "<empty>"
    compact = " ".join(body.split())
    redacted = _SENSITIVE_ERROR_VALUE_RE.sub(r"\1<redacted>", compact)
    return redacted[:limit]


_STREAM_ERROR_HINTS = (
    "resourceexhausted",
    "upstream error",
    "worker local total request limit",
)
_UPSTREAM_PREFETCH_MAX_BYTES = 64 * 1024
_UPSTREAM_PREFETCH_MAX_EVENTS = 4


def _stream_status(value: Any) -> int | None:
    """Extract an HTTP-like status from a provider error payload."""
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _stream_error_from_frame(
    frame: bytes,
    *,
    provider: str,
    model: str,
) -> ProviderError | None:
    """Turn an upstream SSE error envelope into a fallback-eligible error.

    Some OpenAI-compatible proxies return HTTP 200 and put an upstream 5xx
    inside the first SSE event. If that event is passed through, the gateway
    has already committed a 200 response to OMP and cannot select a fallback.
    """
    data_lines = [
        line[5:].lstrip()
        for line in frame.splitlines()
        if line.lower().startswith(b"data:")
    ]
    if not data_lines:
        return None
    payload = b"\n".join(data_lines).strip()
    if not payload or payload == b"[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None

    error_present = parsed.get("error") is not None or parsed.get("errors") is not None
    error_obj = parsed.get("error")
    if error_obj is None:
        error_obj = parsed.get("errors")

    if isinstance(error_obj, list):
        error_obj = error_obj[0] if error_obj else None
    if isinstance(error_obj, dict):
        message = (
            error_obj.get("message")
            or error_obj.get("detail")
            or error_obj.get("type")
            or json.dumps(error_obj, ensure_ascii=False)
        )
        code = error_obj.get("code")
        status = _stream_status(error_obj.get("status")) or _stream_status(code)
    elif error_obj is not None:
        message = str(error_obj)
        code = parsed.get("code")
        status = _stream_status(parsed.get("status")) or _stream_status(code)
    else:
        raw_lower = payload.lower()
        # Do not interpret ordinary assistant content as an error. The hint
        # fallback is only used for payloads without a normal choices array.
        if isinstance(parsed.get("choices"), list) or not any(
            hint.encode() in raw_lower for hint in _STREAM_ERROR_HINTS
        ):
            return None
        message = parsed.get("message") or parsed.get("detail") or payload.decode(
            "utf-8", errors="replace"
        )
        code = parsed.get("code")
        status = _stream_status(parsed.get("status")) or _stream_status(code)

    if not error_present and not message:
        return None
    if not isinstance(message, str):
        message = str(message)
    message = message[:500]
    if is_capacity_error(message) or is_capacity_error(payload.decode("utf-8", errors="replace")):
        group = capacity_group_for_route(provider, model, payload.decode("utf-8", errors="replace"))
        if group is not None:
            exc: ProviderError = ProviderCapacityError(group=group, detail=message)
        else:
            exc = ProviderError(
                message,
                code=str(code) if code is not None else "provider_error",
            )
    else:
        exc = ProviderError(message, code=str(code) if code is not None else "provider_error")
    exc.upstream_status = status or 502
    exc.upstream_body = payload.decode("utf-8", errors="replace")[:2000]
    logger.warning(
        "upstream SSE error envelope provider=%s model=%s status=%s body=%s",
        provider,
        model,
        exc.upstream_status,
        _safe_upstream_body(exc.upstream_body),
    )
    return exc


def _stream_frame_is_ready(frame: bytes) -> bool:
    """Return whether a non-error frame proves useful output has begun."""
    data_lines = [
        line[5:].lstrip()
        for line in frame.splitlines()
        if line.lower().startswith(b"data:")
    ]
    if not data_lines:
        return False
    payload = b"\n".join(data_lines).strip()
    if payload == b"[DONE]":
        return True
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    choices = parsed.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason"):
            return True
        delta = choice.get("delta") or {}
        if isinstance(delta, dict) and any(
            delta.get(key)
            for key in (
                "content",
                "reasoning_content",
                "reasoning",
                "thinking",
                "analysis",
                "reasoning_details",
                "tool_calls",
                "function_call",
            )
        ):
            return True
    return False


def _upstream_failure_cooldown_seconds(exc: BaseException) -> float | None:
    """Return a short model cooldown for retryable stream failures."""
    status = getattr(exc, "upstream_status", None)
    if status is not None:
        try:
            if not 500 <= int(status) < 600:
                return None
        except (TypeError, ValueError):
            return None
    elif not isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientError)):
        return None
    try:
        return max(
            1.0,
            float(os.environ.get("TUSKER_UPSTREAM_FAILURE_COOLDOWN_SECS", "60")),
        )
    except ValueError:
        return 60.0


def _persist_cooldown(
    config: dict[str, Any],
    provider: str,
    model: str,
    seconds: float,
) -> None:
    """Persist a cooldown without making the provider path fail."""
    try:
        from pathlib import Path

        from tusker_gateway.persistent_cooldown import PersistentCooldownStore

        db_path = Path(config.get("quality_db_path", "data/quality.db")).parent / "cooldowns.db"
        PersistentCooldownStore(db_path=db_path).record(provider, model, seconds)
    except Exception:
        pass


def _persist_capacity_group_cooldown(
    config: dict[str, Any],
    group: str,
    seconds: float,
) -> None:
    """Persist a shared capacity quarantine without affecting the request."""
    try:
        from pathlib import Path

        from tusker_gateway.persistent_cooldown import PersistentCooldownStore

        db_path = Path(
            config.get("quality_db_path", "data/quality.db")
        ).parent / "cooldowns.db"
        PersistentCooldownStore(db_path=db_path).record_group(group, seconds)
    except Exception:
        pass


def _capacity_failure_group(
    provider: str,
    model: str,
    exc: BaseException,
) -> str | None:
    """Return a capacity group for an upstream, but not local, failure."""
    if getattr(exc, "capacity_rejected", False):
        return None
    detail = getattr(exc, "upstream_body", None) or str(exc)
    if not is_capacity_error(detail) and not getattr(exc, "capacity_group", None):
        return None
    return (
        getattr(exc, "capacity_group", None)
        or capacity_group_for_route(provider, model, detail)
    )


def _quarantine_capacity_failure(
    config: dict[str, Any],
    provider: str,
    model: str,
    exc: BaseException,
) -> str | None:
    """Quarantine all routes sharing a provider capacity pool."""
    group = _capacity_failure_group(provider, model, exc)
    if group is None:
        return None
    seconds = capacity_cooldown_seconds(getattr(exc, "upstream_body", None))
    tracker = global_tracker()
    tracker.cooldown_group(group, seconds)
    _persist_capacity_group_cooldown(config, group, seconds)
    logger.warning(
        "provider capacity quarantine group=%s seconds=%.0f provider=%s model=%s",
        group,
        seconds,
        provider,
        model,
    )
    return group


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
        "github-copilot-enterprise": {"base_url": "https://copilot-api.sita.ghe.com", "chat_path": "/chat/completions", "auth_type": "oauth", "model_header": "x-github-gpt-model"},
        "openai-codex": {"base_url": "https://api.github.com/copilot", "chat_path": "/chat/completions", "auth_type": "oauth", "model_header": "x-openai-gpt-model"},
        "openai": {"base_url": "https://api.openai.com", "chat_path": "/v1/chat/completions", "auth_type": "bearer"},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "chat_path": "/chat/completions", "auth_type": "bearer"},
        "groq": {"base_url": "https://api.groq.com/openai", "chat_path": "/v1/chat/completions", "auth_type": "bearer"},
        "local-llm": {"base_url": "http://localhost:11434", "chat_path": "/v1/chat/completions", "auth_type": "bearer"},
        "zai": {"base_url": "https://api.z.ai/api/paas", "chat_path": "/v4/chat/completions", "auth_type": "bearer"},
    }


PROVIDER_ENDPOINTS: dict[str, dict[str, Any]] = _init_provider_endpoints()


def _configured_endpoint(config: dict[str, Any], provider: str) -> dict[str, Any] | None:
    """Build an endpoint for a provider supplied through runtime config."""
    registry = config.get("providers")
    if not isinstance(registry, dict):
        return None
    provider_config = registry.get(provider.lower())
    if provider_config is None:
        return None
    if isinstance(provider_config, dict):
        base_url = provider_config.get("base_url")
        chat_path = provider_config.get("chat_path")
        auth_type = provider_config.get("auth_type", provider_config.get("kind", "bearer"))
        model_header = provider_config.get("model_header")
    else:
        base_url = getattr(provider_config, "base_url", None)
        chat_path = getattr(provider_config, "chat_path", None)
        auth_type = getattr(
            provider_config,
            "auth_type",
            getattr(provider_config, "kind", "bearer"),
        )
        model_header = getattr(provider_config, "model_header", None)
    if not base_url or not chat_path:
        return None
    endpoint: dict[str, Any] = {
        "base_url": str(base_url).rstrip("/"),
        "chat_path": str(chat_path),
        "auth_type": str(auth_type or "bearer"),
    }
    if model_header:
        endpoint["model_header"] = str(model_header)
    return endpoint


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


def _chat_content_to_responses(content: Any) -> str | list[dict[str, Any]]:
    """Convert OpenAI chat content into Responses API input content."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        raise ProviderError("Message content must be a string or content array")

    converted: list[dict[str, Any]] = []
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            raise ProviderError(f"Message content block {index} must be an object")
        block_type = block.get("type")
        if block_type in {"text", "input_text"}:
            text = block.get("text")
            if not isinstance(text, str):
                raise ProviderError(f"Message text block {index} must contain a string text field")
            converted.append({"type": "input_text", "text": text})
        elif block_type in {"image_url", "input_image"}:
            image_url = block.get("image_url")
            if isinstance(image_url, dict):
                url = image_url.get("url")
                detail = image_url.get("detail")
            else:
                url = image_url
                detail = block.get("detail")
            if not isinstance(url, str) or not url:
                raise ProviderError(f"Message image block {index} must contain a non-empty image_url")
            image_block: dict[str, Any] = {"type": "input_image", "image_url": url}
            if detail is not None:
                image_block["detail"] = detail
            converted.append(image_block)
        else:
            raise ProviderError(f"Unsupported message content block type: {block_type}")
    return converted


def _responses_text(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        block["text"]
        for block in content
        if block.get("type") == "input_text"
    )


def _responses_tool_choice(tool_choice: Any) -> Any:
    """Translate a Chat Completions tool choice to Responses API shape."""
    if not isinstance(tool_choice, dict):
        return tool_choice
    function = tool_choice.get("function")
    if (
        tool_choice.get("type") == "function"
        and isinstance(function, dict)
        and isinstance(function.get("name"), str)
        and function["name"]
    ):
        return {"type": "function", "name": function["name"]}
    return tool_choice


class PassthroughClient:
    """HTTP client for provider passthrough requests."""

    def __init__(
        self,
        config: dict[str, Any],
        quality_db: QualityDB,
        http_client: aiohttp.ClientSession,
        catalog_registry: Any | None = None,
    ):
        self._config = config
        self._quality = quality_db
        self._http = http_client
        self._catalog_registry = catalog_registry
        self._usage = ProviderUsageDB(
            default_provider_usage_db_path(
                config.get("quality_db_path", "data/quality.db")
            )
        )
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
        self._credential_rotators: dict[str, CodexTokenRotator] = {}
        configured_pools = config.get("credential_pools")
        self._credential_pools_configured = isinstance(configured_pools, dict)
        if isinstance(configured_pools, dict):
            for provider_name, credentials in configured_pools.items():
                if not isinstance(credentials, list):
                    continue
                provider_key = str(provider_name).lower()
                # Environment-provided provider pools are authoritative and
                # should not be written back over the shared auth.json file.
                # Codex keeps persistence because its legacy pool is stored
                # there and the rotator refreshes it in place.
                pool_auth_file = auth_file if provider_key == "openai-codex" else None
                self._credential_rotators[provider_key] = CodexTokenRotator(
                    [credential for credential in credentials if isinstance(credential, dict)],
                    auth_file=pool_auth_file,
                    http_client=http_client,
                )

        self._codex_rotator = self._credential_rotators.get("openai-codex")
        if self._codex_rotator is None:
            self._codex_rotator = CodexTokenRotator(
                codex_creds,
                auth_file=auth_file,
                http_client=http_client,
            )

    def _record_usage(
        self,
        provider: str,
        model: str,
        *,
        success: bool,
        capacity_rejected: bool = False,
        result: dict[str, Any] | None = None,
        group: str | None = None,
    ) -> None:
        """Persist bounded provider usage counters; never affect a request."""
        try:
            usage = result.get("usage") if isinstance(result, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion_tokens = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            self._usage.record(
                provider=provider,
                model=model,
                group=group or capacity_group_for_route(provider, model),
                success=success,
                capacity_rejected=capacity_rejected,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        except Exception:
            logger.debug(
                "provider usage record failed provider=%s model=%s",
                provider,
                model,
                exc_info=True,
            )

    def _reserve_capacity(
        self,
        provider: str,
        model: str,
    ) -> tuple[str | None, CapacityLease | None]:
        """Reserve a local slot or fail fast for a saturated provider group."""
        group = capacity_group_for_route(provider, model)
        if group is None:
            return None, None
        tracker = global_tracker()
        if tracker.is_group_cooldown(group):
            error = ProviderCapacityError(
                group=group,
                capacity_rejected=True,
            )
            self._record_usage(
                provider,
                model,
                success=False,
                capacity_rejected=True,
                group=group,
            )
            raise error
        lease = capacity_controller().acquire(group)
        if lease is None:
            # This is a local admission decision, not an upstream request.
            # Quarantine the group briefly so pool fallback moves directly to
            # another provider instead of trying every sibling Nvidia model.
            busy_window = capacity_busy_cooldown_seconds()
            tracker.cooldown_group(group, busy_window)
            error = ProviderCapacityError(
                group=group,
                capacity_rejected=True,
            )
            self._record_usage(
                provider,
                model,
                success=False,
                capacity_rejected=True,
                group=group,
            )
            raise error
        return group, lease

    @staticmethod
    def _release_capacity(lease: CapacityLease | None) -> None:
        if lease is not None:
            lease.release()

    def _rotator_for(self, provider: str) -> CodexTokenRotator | None:
        """Return the provider-specific credential rotator when configured."""
        provider_key = provider.lower()
        rotators = getattr(self, "_credential_rotators", {})
        if provider_key in rotators:
            return rotators[provider_key]
        # Once the caller supplies a pool map, an absent provider is an
        # intentional empty pool. Falling back to Codex here would recreate
        # the cross-provider credential leak this map is meant to prevent.
        if getattr(self, "_credential_pools_configured", False):
            return None
        return getattr(self, "_codex_rotator", None)

    async def _record_stream_failure(
        self,
        provider: str,
        model: str,
        exc: BaseException,
        started: float,
    ) -> None:
        """Record a stream setup failure and cool down the bad candidate."""
        latency_ms = (time.monotonic() - started) * 1000
        await self._record_quality(provider, model, False, latency_ms)

        body = getattr(exc, "upstream_body", None) or str(exc)
        capacity_group = _quarantine_capacity_failure(
            self._config, provider, model, exc
        )
        self._record_usage(
            provider,
            model,
            success=False,
            group=capacity_group,
        )

        seconds = _upstream_failure_cooldown_seconds(exc)
        if seconds is not None:
            tracker = global_tracker()
            tracker.cooldown(provider, model, seconds)
            _persist_cooldown(self._config, provider, model, seconds)
            if tracker.record_failure(provider):
                provider_seconds = 300.0
                tracker.cooldown(provider, "", provider_seconds)
                _persist_cooldown(self._config, provider, "", provider_seconds)

        logger.warning(
            "provider stream setup failed provider=%s model=%s status=%s cooldown=%s body=%s",
            provider,
            model,
            getattr(exc, "upstream_status", None),
            f"{seconds:.0f}s" if seconds is not None else "none",
            _safe_upstream_body(body),
        )

    async def chat(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        *,
        stream: bool = False,
        api_key: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        upstream_gateway: str | None = None,
        rtk_compress: bool = True,
        metrics_registry: Any | None = None,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        """Make a passthrough chat completions call to the provider."""
        logger.info('passthrough %s/%s stream=%s', provider, model, stream)

        # Optional RTK compression: trims verbose tool_result content (git
        # diffs, ls dumps, test runner output) before dispatch. Default ON
        # when the global RTK flag is set; pass rtk_compress=False to skip.
        if rtk_compress and messages:
            from tusker_gateway.rtk import compress_tool_results, is_enabled as rtk_enabled
            if rtk_enabled():
                messages = compress_tool_results(messages, metrics=metrics_registry)

        # Resolve the endpoint up front so the dispatch can decide between
        # the standard chat-completions passthrough and the openai-codex
        # Responses API adapter.
        if upstream_gateway:
            endpoint = {"base_url": upstream_gateway.rstrip("/"), "chat_path": "/v1/chat/completions", "auth_type": "bearer"}
        else:
            endpoint = PROVIDER_ENDPOINTS.get(provider)
            if endpoint is None:
                endpoint = _configured_endpoint(self._config, provider)
            if not endpoint:
                raise ProviderError(f"Unknown provider: {provider}")

        # Determine whether this model must go through the Responses API
        # adapter (rather than the plain chat-completions passthrough).
        #
        # Precedence:
        #   1. The endpoint's chat_path is already "/responses"
        #      (openai-codex, or a test patch pointing a provider at
        #      the Responses adapter).
        #   2. The catalog says the model's supported_endpoints include
        #      "/responses". GitHub Copilot serves its GPT-5.x codex-
        #      family models (gpt-5.6-luna, gpt-5.5, ...) via the
        #      Responses API even though the provider's default chat
        #      path is /chat/completions; the catalog is the source of
        #      truth for which endpoint a model accepts.
        use_responses = endpoint.get("chat_path", "").endswith("/responses")
        if not use_responses and self._catalog_registry is not None:
            entries = self._catalog_registry.entries_for(provider)
            if entries:
                for entry in entries:
                    if entry.model == model and "/responses" in entry.raw.get(
                        "supported_endpoints", []
                    ):
                        use_responses = True
                        # Copilot models expose "/responses" but not
                        # "/chat/completions"; override the path so the
                        # base_url is preserved and the adapter posts to
                        # the Responses endpoint.
                        endpoint = {
                            **endpoint,
                            "chat_path": "/responses",
                        }
                        break

        if use_responses:
            return await self._chat_codex(
                provider, model, messages,
                stream=stream, api_key=api_key, tools=tools,
                tool_choice=tool_choice,
                extra_headers=extra_headers, extra_body=extra_body,
                endpoint=endpoint,
            )

        base_url = endpoint["base_url"]
        path = endpoint["chat_path"]
        url = f"{base_url}{path}"
        headers, body = await self._build_request(
            provider, model, messages,
            stream=stream, api_key=(self._config["api_keys"][0] if upstream_gateway else api_key),
            tools=tools, tool_choice=tool_choice,
            extra_headers=extra_headers, extra_body=extra_body,
            endpoint=endpoint,
        )

        capacity_group, capacity_lease = self._reserve_capacity(provider, model)
        start = time.monotonic()
        if stream:
            try:
                resp = await self._http.request(
                    "POST", url, headers=headers, json=body,
                    timeout=aiohttp.ClientTimeout(
                        total=120,
                        # Cap the *gap* between SSE bytes. If the provider goes
                        # silent for this long, aiohttp raises a clean
                        # asyncio.TimeoutError instead of letting the socket
                        # hang until the 120s `total` fires.
                        sock_read=_UPSTREAM_STREAM_SOCK_READ_SECS,
                    ),
                )
            except Exception as exc:
                self._release_capacity(capacity_lease)
                await self._record_stream_failure(provider, model, exc, start)
                raise
            try:
                await self._check_response(resp, provider=provider, model=model)
                prefetched_chunks, upstream_iterator = await self._prefetch_stream(
                    resp,
                    provider=provider,
                    model=model,
                )
            except RateLimitError as exc:
                from tusker_gateway.cooldown import _cooldown_seconds_for_429
                tracker = global_tracker()
                body_text = exc.body or "429 rate limit"
                seconds = _cooldown_seconds_for_429({"body": body_text, "headers": dict(resp.headers)})
                logger.warning('429 from %s/%s, cooldown=%.0fs', provider, model, seconds)
                tracker.cooldown(provider, model, seconds)
                self._record_usage(
                    provider,
                    model,
                    success=False,
                    group=capacity_group,
                )
                self._release_capacity(capacity_lease)
                resp.release()
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
                await self._record_stream_failure(provider, model, exc, start)
                self._release_capacity(capacity_lease)
                resp.release()
                raise
            # Record quality on streaming success
            try:
                latency_ms = (time.monotonic() - start) * 1000
                await self._record_quality(provider, model, True, latency_ms)
                self._record_usage(
                    provider,
                    model,
                    success=True,
                    group=capacity_group,
                )
                global_tracker().clear_failures(provider)
            except Exception:
                pass
            return self._stream_events(
                resp,
                provider=provider,
                model=model,
                initial_chunks=prefetched_chunks,
                stream_iterator=upstream_iterator,
                capacity_lease=capacity_lease,
            )
        try:
            async with self._http.request(
                "POST", url, headers=headers, json=body,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                await self._check_response(resp, provider=provider, model=model)
                result = await resp.json()
                from tusker_gateway.tool_formats import normalize_response_tool_calls
                result = normalize_response_tool_calls(
                    result,
                    source=f"{provider}/{model}",
                )
                self._record_usage(
                    provider,
                    model,
                    success=True,
                    result=result,
                    group=capacity_group,
                )
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
            self._record_usage(
                provider,
                model,
                success=False,
                group=capacity_group,
            )
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
            _quarantine_capacity_failure(self._config, provider, model, exc)
            self._record_usage(
                provider,
                model,
                success=False,
                group=capacity_group,
            )
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
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(str(exc)) from exc
        finally:
            self._release_capacity(capacity_lease)

    async def _prefetch_stream(
        self,
        resp: aiohttp.ClientResponse,
        *,
        provider: str,
        model: str,
    ) -> tuple[list[bytes], AsyncIterator[bytes]]:
        """Read enough of an upstream stream to catch an immediate error.

        The consumed bytes and the same iterator are returned so successful
        streams are byte-for-byte preserved. The bounded prefetch prevents a
        malformed provider response without SSE separators from buffering an
        unbounded amount of data.
        """
        upstream_iterator = resp.content.iter_any().__aiter__()
        prefetched: list[bytes] = []
        pending = bytearray()
        reached_eof = False
        prefetched_bytes = 0
        event_count = 0

        # Role-only and metadata events are common before the first token.
        # Keep reading through those events so a proxy cannot hide an
        # immediate provider error behind an otherwise successful HTTP 200.
        while (
            event_count < _UPSTREAM_PREFETCH_MAX_EVENTS
            and prefetched_bytes < _UPSTREAM_PREFETCH_MAX_BYTES
        ):
            while b"\n\n" not in pending and prefetched_bytes < _UPSTREAM_PREFETCH_MAX_BYTES:
                try:
                    chunk = await upstream_iterator.__anext__()
                except StopAsyncIteration:
                    reached_eof = True
                    break
                if not chunk:
                    continue
                prefetched.append(chunk)
                prefetched_bytes += len(chunk)
                pending.extend(chunk)

            if b"\n\n" not in pending:
                if reached_eof or prefetched_bytes >= _UPSTREAM_PREFETCH_MAX_BYTES:
                    frame = bytes(pending)
                    pending.clear()
                    error = _stream_error_from_frame(
                        frame,
                        provider=provider,
                        model=model,
                    )
                    if error is not None:
                        raise error
                break

            frame, remainder = pending.split(b"\n\n", 1)
            pending = bytearray(remainder)
            event_count += 1
            error = _stream_error_from_frame(
                frame,
                provider=provider,
                model=model,
            )
            if error is not None:
                raise error
            if _stream_frame_is_ready(frame):
                break

        return prefetched, upstream_iterator

    async def _build_request(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        *,
        stream: bool,
        api_key: str | None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        extra_headers: dict[str, str] | None,
        extra_body: dict[str, Any] | None,
        endpoint: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        from tusker_gateway.auth_strategies import get_auth_strategy
        from tusker_gateway.models import ProviderConfig
        from tusker_gateway.tool_formats import normalize_tools

        endpoint_model = ProviderConfig.from_raw(endpoint)
        strategy = get_auth_strategy(
            endpoint_model.auth_type,
            self._rotator_for(provider),
        )
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
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if extra_body:
            body.update(extra_body)
        return headers, body
    async def _chat_codex(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        *,
        stream: bool,
        api_key: str | None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        endpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        from tusker_gateway.auth_strategies import get_auth_strategy
        from tusker_gateway.models import ProviderConfig
        from tusker_gateway.tool_formats import normalize_tools
        endpoint_raw = endpoint or PROVIDER_ENDPOINTS["openai-codex"]
        endpoint_model = ProviderConfig.from_raw(endpoint_raw)
        rotator = self._rotator_for(provider)
        # Honor the patched endpoint's auth_type (test-only override path).
        # Default to "codex" so production keeps its dedicated auth strategy.
        auth_type = endpoint_raw.get("auth_type") or endpoint_model.auth_type or "codex"
        if auth_type == "codex":
            strategy = get_auth_strategy("codex", rotator)
        elif auth_type == "oauth":
            strategy = get_auth_strategy("oauth", rotator)
        else:
            strategy = get_auth_strategy("bearer", getattr(self, "_codex_rotator", None))
        headers = {
            "Content-Type": "application/json",
            **(extra_headers or {}),
            **await strategy.headers(self._config, provider, model, api_key, endpoint_model),
        }
        input_data: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                continue
            content = _chat_content_to_responses(msg.get("content"))
            input_role = "developer" if role == "developer" else role
            if input_role == "assistant" and isinstance(content, str):
                # Keep the existing assistant-history wire shape. Responses
                # accepts assistant input as a plain string; output_text is
                # reserved for model output items.
                input_data.append({"role": input_role, "content": content})
            else:
                if isinstance(content, str):
                    content = [{"type": "input_text", "text": content}]
                input_data.append({"role": input_role, "content": content})
        # Codex backend requires stream=true; force it here regardless of
        # what the caller asked for (the response parser handles SSE).
        body: dict[str, Any] = {
            "model": model,
            "input": input_data,
            "stream": True,
            "store": False,
        }
        # Pull the first system/developer message into `instructions` if present.
        # Multimodal system content keeps its image in `input`; only text is
        # duplicated into instructions.
        if messages and messages[0].get("role") in {"system", "developer"}:
            sys_content = _chat_content_to_responses(messages[0].get("content"))
            sys_text = _responses_text(sys_content).strip()
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
            body["tool_choice"] = (
                _responses_tool_choice(tool_choice)
                if tool_choice is not None
                else "auto"
            )
            body["parallel_tool_calls"] = True
        if extra_body:
            # Forward everything from extra_body upstream except the
            # chat-completions-only params the Codex Responses API rejects.
            # The Codex backend for some privacy-pool models
            # (gpt-5.4-mini, gpt-5.6-luna) rejects max-tokens and
            # stream_options; it applies its own defaults when omitted,
            # so silently dropping is safer than guessing.
            mapped = dict(extra_body)
            for k in (
                "max_tokens",
                "max_completion_tokens",
                "max_output_tokens",
                # Codex Responses streams usage via `response.completed`
                # events rather than chat-completions' stream_options;
                # some Codex model deployments reject the latter as an
                # unknown parameter.
                "stream_options",
            ):
                mapped.pop(k, None)
            # The Codex Responses API accepts reasoning as a nested object
            # ({effort, summary}) — not the flat chat-completions
            # `reasoning_effort` top-level field. Fold the client's value
            # into the nested object so the request isn't rejected with
            # "Unsupported parameter: reasoning_effort".
            reffort = mapped.pop("reasoning_effort", None)
            if reffort is not None:
                if not isinstance(body.get("reasoning"), dict):
                    body["reasoning"] = {}
                body["reasoning"]["effort"] = reffort
            body.update(mapped)
        url = f"{endpoint_raw['base_url']}{endpoint_raw['chat_path']}"
        start = time.monotonic()
        resp = await self._http.request("POST", url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=120))
        try:
            await self._check_response(resp, provider=provider, model=model)
        except RateLimitError as exc:
            resp.release()
            from tusker_gateway.cooldown import _cooldown_seconds_for_429
            tracker = global_tracker()
            seconds = _cooldown_seconds_for_429({"body": (exc.body or "429"), "headers": {}})
            tracker.cooldown(provider, model, seconds)
            try:
                from tusker_gateway.persistent_cooldown import PersistentCooldownStore
                from pathlib import Path
                db_path = Path(self._config.get("quality_db_path", "data/quality.db")).parent / "cooldowns.db"
                PersistentCooldownStore(db_path=db_path).record(provider, model, seconds)
            except Exception:
                pass
            # Advance to next credential so the next request doesn't keep
            # hammering a rate-limited account.
            if rotator and rotator.size > 1:
                await rotator.advance()
            raise
        except Exception as exc:
            # Use the body/status attached by _check_response (raised above)
            # instead of re-reading the response — aiohttp's resp.text() may
            # have already been consumed. Auth failures (401/403) are ERROR
            # level since a credential/model is broken and needs attention.
            status = getattr(exc, "upstream_status", None)
            body_text = _safe_upstream_body(getattr(exc, "upstream_body", None))
            if status in (401, 403):
                logger.error(
                    "codex auth error model=%s stream=%s status=%d body=%s",
                    model, stream, status or 0, body_text[:300],
                )
            elif status is not None:
                logger.warning(
                    "codex error model=%s stream=%s status=%d body=%s",
                    model, stream, status, body_text[:300],
                )
            else:
                logger.error(
                    "codex error model=%s stream=%s body=%s",
                    model, stream, body_text[:300],
                )
            resp.release()
            # Advance to next credential on any non-2xx so a sick account
            # doesn't cause the breaker to trip after 5 consecutive failures.
            if rotator and rotator.size > 1:
                await rotator.advance()
            raise
        result = await self._parse_codex_sse_async(resp)
        from tusker_gateway.tool_formats import normalize_response_tool_calls

        result = normalize_response_tool_calls(
            result,
            source=f"{provider}/{model}",
        )
        latency_ms = (time.monotonic() - start) * 1000
        await self._record_quality(provider, model, True, latency_ms)
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
    async def _check_response(
        resp: aiohttp.ClientResponse,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        if resp.status == 200:
            return
        body = await resp.text()
        logger.warning(
            "provider response error provider=%s model=%s status=%d body=%s",
            provider or "unknown",
            model or "unknown",
            resp.status,
            _safe_upstream_body(body),
        )
        if is_capacity_error(body):
            group = capacity_group_for_route(provider or "", model or "", body)
            if group is not None:
                capacity_error = ProviderCapacityError(
                    group=group,
                    detail=_safe_upstream_body(body, limit=500),
                )
                capacity_error.upstream_status = resp.status
                capacity_error.upstream_body = body
                raise capacity_error
        if resp.status == 429:
            raise RateLimitError(body=body, headers=dict(resp.headers))
        if resp.status == 401:
            exc = ProviderError("Provider authentication failed", code="auth_error")
        elif resp.status == 403:
            exc = ProviderError("Provider access forbidden", code="forbidden")
        elif resp.status >= 500:
            exc = ProviderError(f"Provider returned {resp.status}: {body[:200]}", code="provider_error")
        else:
            exc = ProviderError(f"Provider returned {resp.status}: {body[:200]}", code="provider_error")
        # Carry the upstream status/body so the circuit-breaker failure path
        # can derive a sensible cooldown for permanent (401/403/404) failures.
        exc.upstream_status = resp.status
        exc.upstream_body = body
        raise exc

    async def _stream_events(
        self,
        resp: aiohttp.ClientResponse,
        *,
        provider: str | None = None,
        model: str | None = None,
        initial_chunks: list[bytes] | None = None,
        stream_iterator: AsyncIterator[bytes] | None = None,
        capacity_lease: CapacityLease | None = None,
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
            for chunk in initial_chunks or []:
                yield chunk
            iterator = stream_iterator or resp.content.iter_any()
            async for chunk in iterator:
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
            self._release_capacity(capacity_lease)

    async def _record_quality(
        self, provider: str, model: str, success: bool, latency_ms: float
    ) -> None:
        try:
            if hasattr(self._quality, "record"):
                await self._quality.record(provider, model, success, latency_ms)
        except Exception:
            pass  # Quality DB is best-effort
