"""Bounded, opt-in qualification for embedding and rerank routes.

Qualification runs outside the request path and records only status metadata in
ModelCapabilityDB. It sends one small request per configured provider/model,
never stores prompts, documents, vectors, or provider response bodies, and
keeps transient provider failures retryable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

from tusker_gateway.config import load_config
from tusker_gateway.model_capability import ModelCapabilityDB
from tusker_gateway.providers.embed import EmbedHandler
from tusker_gateway.providers.rerank import RerankHandler

logger = logging.getLogger(__name__)

MEDIA_CAPABILITY_PROBE_VERSION = "media-capability-v1"
EMBEDDING_CAPABILITY = "embedding"
RERANK_CAPABILITY = "rerank"


def _status_from_response(status: int, payload: Any, capability: str) -> tuple[str, str | None]:
    """Classify a media probe without retaining upstream response data."""
    if 200 <= status < 300:
        if capability == EMBEDDING_CAPABILITY and isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return "passed", None
        if capability == RERANK_CAPABILITY and isinstance(payload, dict) and isinstance(payload.get("results"), list):
            return "passed", None
        return "unavailable", "invalid_response"
    if status in {401, 403} or status == 429 or status >= 500:
        return "unavailable", "provider_unavailable"
    code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
    if code in {"unsupported_model", "unsupported_provider", "missing_api_key", "no_embed_providers", "no_reranker_providers"}:
        return "unsupported", str(code)
    return "unavailable", "provider_rejected"


def _request_for(capability: str, provider: str, model: str) -> tuple[str, dict[str, Any]]:
    pin = f"{provider}::{model}"
    if capability == EMBEDDING_CAPABILITY:
        return "/v1/embeddings", {"model": pin, "input": "qualification"}
    if capability == RERANK_CAPABILITY:
        return "/v1/rerank", {"model": pin, "query": "qualification", "documents": ["qualification"]}
    raise ValueError(f"unsupported media capability: {capability}")


async def probe_media_route(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    api_key: str,
    provider: str,
    model: str,
    capability: str,
    timeout_secs: float = 45.0,
) -> dict[str, Any]:
    """Probe one configured media route and return safe evidence metadata."""
    path, payload = _request_for(capability, provider, model)
    result: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "capability": capability,
        "status": "unavailable",
        "source": "media_probe",
        "probe_version": MEDIA_CAPABILITY_PROBE_VERSION,
        "http_status": None,
        "latency_ms": None,
        "failure_class": None,
    }
    started = time.monotonic()
    try:
        async with session.post(
            f"{base_url.rstrip('/')}{path}",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Tusker-Media-Qualification": MEDIA_CAPABILITY_PROBE_VERSION,
            },
            timeout=aiohttp.ClientTimeout(total=timeout_secs),
        ) as response:
            result["http_status"] = response.status
            try:
                body = await response.json()
            except (TypeError, ValueError, json.JSONDecodeError):
                body = None
            result["status"], result["failure_class"] = _status_from_response(
                response.status, body, capability
            )
    except asyncio.TimeoutError:
        result["failure_class"] = "timeout"
    except (aiohttp.ClientError, OSError) as exc:
        result["failure_class"] = type(exc).__name__
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def _candidate_routes(config: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return usable configured embed/rerank provider/model pairs."""
    embedder = EmbedHandler(config)
    reranker = RerankHandler(config)
    candidates = [
        (provider, model, EMBEDDING_CAPABILITY)
        for provider, model in embedder._configured_models()
    ]
    candidates.extend(
        (provider, model, RERANK_CAPABILITY)
        for provider, model in reranker._configured_models()
    )
    return candidates


async def run_media_qualification_cycle(
    *,
    base_url: str = "http://127.0.0.1:8642",
    limit: int = 8,
    timeout_secs: float = 45.0,
    credential_rotators: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Probe a bounded set of configured media routes and persist evidence."""
    del credential_rotators  # Reserved for parity with other qualification loops.
    config = load_config()
    api_key = os.environ.get("API_KEYS", "").split(",", 1)[0].strip()
    if not api_key:
        raise RuntimeError("API_KEYS must contain the gateway caller key")
    db = ModelCapabilityDB(
        config.get("model_capability_db_path")
        or str(Path(config.get("quality_db_path", "data/quality.db")).with_name("model_capability.db"))
    )
    candidates = _candidate_routes(config)[: max(0, limit)]
    counts: dict[str, int] = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_secs)) as session:
        for provider, model, capability in candidates:
            result = await probe_media_route(
                session,
                base_url=base_url,
                api_key=api_key,
                provider=provider,
                model=model,
                capability=capability,
                timeout_secs=timeout_secs,
            )
            db.record(
                provider=provider,
                model=model,
                capability=capability,
                status=result["status"],
                source="media_probe",
                probe_version=MEDIA_CAPABILITY_PROBE_VERSION,
                http_status=result.get("http_status"),
                latency_ms=result.get("latency_ms"),
                failure_class=result.get("failure_class"),
            )
            key = f"{capability}:{result['status']}"
            counts[key] = counts.get(key, 0) + 1
    return {"tested": len(candidates), "counts": counts}


async def media_qualification_loop(
    stop_event: asyncio.Event,
    *,
    base_url: str = "http://127.0.0.1:8642",
    credential_rotators: dict[str, Any] | None = None,
) -> None:
    """Run opt-in media qualification on a slow cadence."""
    try:
        initial_delay = max(0.0, float(os.environ.get("TUSKER_MEDIA_QUALIFICATION_INITIAL_DELAY_SECS", "600")))
    except ValueError:
        initial_delay = 600.0
    try:
        interval = max(60.0, float(os.environ.get("TUSKER_MEDIA_QUALIFICATION_INTERVAL_SECS", "43200")))
    except ValueError:
        interval = 43_200.0
    try:
        limit = max(1, int(os.environ.get("TUSKER_MEDIA_QUALIFICATION_LIMIT", "8")))
    except ValueError:
        limit = 8
    try:
        timeout = max(5.0, float(os.environ.get("TUSKER_MEDIA_QUALIFICATION_TIMEOUT_SECS", "45")))
    except ValueError:
        timeout = 45.0
    if initial_delay and await _wait_or_stop(stop_event, initial_delay):
        return
    while not stop_event.is_set():
        try:
            summary = await run_media_qualification_cycle(
                base_url=base_url,
                limit=limit,
                timeout_secs=timeout,
                credential_rotators=credential_rotators,
            )
            logger.info("media qualification result=%s", summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("media qualification cycle failed error_class=%s", type(exc).__name__)
        if await _wait_or_stop(stop_event, interval):
            return


async def _wait_or_stop(stop_event: asyncio.Event, delay: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        return False
    return True


__all__ = [
    "EMBEDDING_CAPABILITY",
    "MEDIA_CAPABILITY_PROBE_VERSION",
    "RERANK_CAPABILITY",
    "media_qualification_loop",
    "probe_media_route",
    "run_media_qualification_cycle",
]
