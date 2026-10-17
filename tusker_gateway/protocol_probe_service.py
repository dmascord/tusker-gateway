"""Authenticated admin protocol-probe service.

Binds probe executors to *configured* endpoints and their existing auth
strategies. There is no client-controlled URL: the endpoint comes from the
provider registry / runtime config, exactly like the production request
path, and the executor performs a single attempt with no pool fallback.

Raw request/response bodies are never returned or persisted; only the
classified feature/status evidence from the probe modules is recorded.
"""
from collections.abc import AsyncIterator
import asyncio
import logging
from typing import Any

import aiohttp

from tusker_gateway.protocol_evidence import ProtocolEvidenceStore
from tusker_gateway.protocol_parameter_probe import probe_parameter
from tusker_gateway.protocol_probe_coordinator import ProbeCoordinator
from tusker_gateway.protocol_probe_policy import authorize_probe_target
from tusker_gateway.protocol_probe_runner import probe_chat_contract
from tusker_gateway.protocol_replay_probe import probe_reasoning_replay
from tusker_gateway.protocol_responses_roundtrip import probe_responses_roundtrip
from tusker_gateway.protocol_stream_probe import probe_streaming_tool_fragments

logger = logging.getLogger(__name__)

_SINGLE_TURN_FEATURES = ("structured_tool_calls", "tool_call_finish", "reasoning_channel")
_PARAMETER_FEATURES = ("reasoning_effort", "thinking_parameter")
_ALL_FEATURES = frozenset({
    *_SINGLE_TURN_FEATURES, "reasoning_replay", "streaming_tool_fragments",
    "responses_function_calls", *_PARAMETER_FEATURES,
})

# Operator gate for billable live probes. Without it the route answers with
# a policy error before any upstream traffic is generated.
PROBES_ENABLED_ENV = "TUSKER_PROTOCOL_PROBES_ENABLED"


class ProtocolProbeError(Exception):
    """Operator-facing probe policy failure (not an upstream failure)."""

    def __init__(self, message: str, *, status: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


def probes_enabled(env: dict[str, str] | None = None) -> bool:
    import os

    source = os.environ if env is None else env
    return source.get(PROBES_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def app_coordinator(app: Any) -> ProbeCoordinator:
    """Process-wide probe coordination: bounded concurrency + per-target cooldown."""
    coordinator = app.get("protocol_probe_coordinator")
    if coordinator is None:
        try:
            max_concurrent = max(1, int(app["config"].get("protocol_probe_max_concurrent", 2)))
        except (KeyError, TypeError, ValueError):
            max_concurrent = 2
        coordinator = ProbeCoordinator(max_concurrent=max_concurrent, cooldown_seconds=300.0)
        app["protocol_probe_coordinator"] = coordinator
    return coordinator


def probe_target_from_request(
    config: dict[str, Any], payload: dict[str, Any]
) -> tuple[str, str, dict[str, Any], str]:
    """Resolve and authorize the probe target from an admin request body.

    Returns (provider, model, endpoint, endpoint_id). Raises ProtocolProbeError
    for unconfigured providers/models. The model must be in the provider's
    static aliases or the pool rosters so arbitrary model strings cannot
    broaden the probe surface.
    """
    provider = str(payload.get("provider") or "").strip()
    model = str(payload.get("model") or "").strip()
    if not provider or not model:
        raise ProtocolProbeError("provider and model are required")
    registry = config.get("providers") or {}
    provider_config = registry.get(provider.lower())
    if provider_config is None:
        raise ProtocolProbeError(f"unconfigured provider: {provider}", status=404, code="unknown_provider")
    from tusker_gateway.passthrough import _configured_endpoint

    endpoint = _configured_endpoint(config, provider)
    if endpoint is None:
        raise ProtocolProbeError(
            f"provider {provider} has no chat endpoint configured",
            status=404, code="unknown_endpoint",
        )
    if not _model_known(config, provider, model):
        raise ProtocolProbeError(
            f"model {model} is not in the {provider} roster", status=404, code="unknown_model",
        )
    from tusker_gateway.protocol_runtime import endpoint_identity

    return provider, model, endpoint, endpoint_identity(endpoint)


def _model_known(config: dict[str, Any], provider: str, model: str) -> bool:
    provider_config = (config.get("providers") or {}).get(provider.lower())
    aliases = (
        provider_config.get("model_aliases", {})
        if isinstance(provider_config, dict)
        else getattr(provider_config, "model_aliases", {})
    ) or {}
    if model in aliases:
        return True
    from tusker_gateway.pools import PoolManager
    from tusker_gateway.config import load_config

    pools = PoolManager(config if config.get("models") else load_config())
    for pool in pools.status().get("pools", []):
        for entry in pool.get("models", []):
            provider_name = str(entry.get("provider") or "")
            model_name = str(entry.get("model") or "")
            if provider_name.lower() == provider.lower() and model_name == model:
                return True
    return False



class _BoundExecutor:
    """Single-endpoint chat executor used by every probe feature.

    One aiohttp POST per call, bound to the configured endpoint URL and its
    production auth headers. No redirects across hosts (SSRF guard), no pool
    fallback, no retries beyond the caller's single wait_for.
    """

    def __init__(
        self, *, url: str, headers: dict[str, str],
        session: aiohttp.ClientSession, model_header: str | None,
        upstream_model: str,
    ):
        self._url = url
        self._headers = headers
        self._session = session
        self._model_header = model_header
        self._upstream_model = upstream_model
        self._timeout_json = aiohttp.ClientTimeout(total=60, sock_read=55)
        self._timeout_sse = aiohttp.ClientTimeout(total=60, sock_read=30)

    def _prepare(self, request: dict[str, Any]) -> dict[str, Any]:
        body = dict(request)
        if self._model_header and "model" in body:
            # Providers that select the model via header instead of body.
            body.pop("model", None)
        return body
    async def _post(self, body: dict[str, Any]) -> aiohttp.ClientResponse:
        return await self._session.post(
            self._url, json=body, headers=self._headers,
            timeout=self._timeout_json, allow_redirects=False, max_redirects=0,
        )

    async def chat_json(self, request: dict[str, Any]) -> dict[str, Any]:
        async with await self._post(self._prepare(request)) as resp:
            data = await resp.json(content_type=None)
            if resp.status == 429 or resp.status >= 500:
                # Transient upstream state: probes must record "unavailable",
                # not "unsupported". Matches classify_probe's transient set.
                raise ConnectionError(f"upstream status {resp.status}")
            if isinstance(data, dict):
                return data
            return {}

    async def chat_sse(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        body = self._prepare(request)
        body["stream"] = True
        resp = await self._session.post(
            self._url, json=body, headers=self._headers,
            timeout=self._timeout_sse, allow_redirects=False, max_redirects=0,
        )
        if resp.status == 429 or resp.status >= 500:
            resp.release()
            raise ConnectionError(f"upstream status {resp.status}")
        if resp.status >= 400:
            resp.release()
            return _rejected_stream()

        async def _it() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.content.iter_any():
                    yield chunk
            finally:
                resp.release()

        return _it()

    async def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.chat_json(request)


async def _rejected_stream() -> AsyncIterator[bytes]:
    yield b'data: {"choices": []}\n\n'
    yield b"data: [DONE]\n\n"


async def _error_stream(status: int) -> AsyncIterator[bytes]:
    yield f'data: {{"__http_status__": {status}}}\n\n'.encode()


async def run_probe(
    app: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    """Execute approved probes for one provider/model against its endpoint."""
    config = app["config"]
    if not probes_enabled():
        raise ProtocolProbeError(
            f"{PROBES_ENABLED_ENV} is not enabled; live probes are disabled",
            status=403, code="probes_disabled",
        )
    if payload.get("approved") is not True:
        raise ProtocolProbeError(
            "operator approval flag required", status=403, code="approval_required",
        )
    provider, model, endpoint, endpoint_id = probe_target_from_request(config, payload)
    requested = payload.get("features")
    if requested is None:
        features = sorted(_ALL_FEATURES - {"responses_function_calls"})
    else:
        if not isinstance(requested, list) or not requested or not all(
            isinstance(f, str) and f in _ALL_FEATURES for f in requested
        ):
            raise ProtocolProbeError(
                f"features must be a non-empty subset of {sorted(_ALL_FEATURES)}",
            )
        features = sorted(set(requested))
    try:
        timeout_seconds = float(payload.get("timeout_seconds", 15.0))
    except (TypeError, ValueError):
        raise ProtocolProbeError("timeout_seconds must be a number")
    if not (1.0 <= timeout_seconds <= 60.0):
        raise ProtocolProbeError("timeout_seconds must be between 1 and 60")

    coordinator = app_coordinator(app)

    async def operation() -> dict[str, Any]:
        store = ProtocolEvidenceStore()
        session: aiohttp.ClientSession = app["http_session"]
        from tusker_gateway.passthrough import PassthroughClient
        from tusker_gateway.quality import QualityDB

        client = PassthroughClient(
            config, QualityDB(config["quality_db_path"]), session,
            catalog_registry=app.get("catalog_registry"),
            credential_rotators=app.get("credential_rotators"),
        )
        provider_config = (config.get("providers") or {}).get(provider.lower()) or {}
        aliases = (
            provider_config.get("model_aliases", {})
            if isinstance(provider_config, dict)
            else getattr(provider_config, "model_aliases", {})
        ) or {}
        upstream_model = aliases.get(model, model)
        headers, _ = await client._build_request(
            provider, upstream_model, [{"role": "user", "content": "x"}],
            stream=False, api_key=None, extra_headers={}, extra_body={},
            endpoint=endpoint,
        )
        executor = _BoundExecutor(
            url=f"{endpoint['base_url']}{endpoint['chat_path']}",
            headers=headers,
            session=session,
            model_header=endpoint.get("model_header"),
            upstream_model=upstream_model,
        )
        results: dict[str, Any] = {}
        # Group single-turn features into one bounded request.
        single = [f for f in features if f in _SINGLE_TURN_FEATURES]
        if single:
            assessments = await probe_chat_contract(
                provider=provider, endpoint_id=endpoint_id, model=model,
                execute=executor, store=store, timeout_seconds=timeout_seconds,
            )
            results.update(assessments)
        if "reasoning_replay" in features:
            results["reasoning_replay"] = await probe_reasoning_replay(
                provider=provider, endpoint_id=endpoint_id, model=model,
                execute=executor, store=store, timeout_seconds=timeout_seconds,
            )
        if "streaming_tool_fragments" in features:
            results["streaming_tool_fragments"] = await probe_streaming_tool_fragments(
                provider=provider, endpoint_id=endpoint_id, model=model,
                execute=executor.chat_sse, store=store,
                timeout_seconds=timeout_seconds,
            )
        if "responses_function_calls" in features:
            responses_url = f"{endpoint['base_url']}/responses"
            responses_executor = _BoundExecutor(
                url=responses_url, headers=headers, session=session,
                model_header=endpoint.get("model_header"),
                upstream_model=upstream_model,
            )
            results["responses_function_calls"] = await probe_responses_roundtrip(
                provider=provider, endpoint_id=endpoint_id, model=model,
                execute=responses_executor, store=store,
                timeout_seconds=timeout_seconds,
            )
        for feature in _PARAMETER_FEATURES:
            if feature in features:
                results[feature] = await probe_parameter(
                    provider=provider, endpoint_id=endpoint_id, model=model,
                    feature=feature, execute=executor, store=store,
                    timeout_seconds=timeout_seconds,
                )
        return results

    try:
        results = await coordinator.run(
            provider=provider, endpoint_id=endpoint_id, model=model,
            approved=True, operation=operation,
        )
    except RuntimeError as exc:
        raise ProtocolProbeError(str(exc), status=429, code="cooldown_active")
    return {
        "provider": provider,
        "model": model,
        "endpoint_id": endpoint_id,
        "results": {
            feature: {"status": a.status, "failure_class": a.failure_class}
            for feature, a in results.items()
        },
    }


def evidence_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only evidence dump for operators. No payloads, no credentials."""
    from tusker_gateway.protocol_evidence import (
        MAX_EVIDENCE_AGE_SECONDS,
        PROTOCOL_PROBE_VERSION,
    )

    store = ProtocolEvidenceStore()
    rows = store.all()
    now = __import__("time").time()
    return {
        "probe_version": PROTOCOL_PROBE_VERSION,
        "max_age_seconds": MAX_EVIDENCE_AGE_SECONDS,
        "evidence": [
            {
                "provider": r["provider"], "endpoint_id": r["endpoint_id"],
                "model": r["model"], "feature": r["feature"], "status": r["status"],
                "checked_at": r["checked_at"],
                "verified": now - r["checked_at"] <= MAX_EVIDENCE_AGE_SECONDS
                and r["status"] == "passed",
            }
            for r in rows
        ],
    }
