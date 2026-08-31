"""Qualify chat input modalities through the gateway boundary.

This is an explicit, low-concurrency operation. It tests a concrete
provider/model route and stores only the result in ``ModelCapabilityDB``. The
default image probe is restricted to models whose catalog advertises image
input; pass ``--include-unadvertised`` when an operator wants to investigate
an unknown provider claim.

Examples::

    python -m tusker_gateway.modality_qualification --input-modality image
    tusker-gateway-qualify-modalities --all-pools --input-modality image
    tusker-gateway-qualify-modalities --provider synthetic --include-unadvertised

The runner intentionally does not probe image/audio/video generation. Those
calls can be billable or create asynchronous jobs; use the existing provider
capability discovery for non-billable catalog/endpoint evidence and add an
explicit provider-approved generation probe before enabling such calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

from tusker_gateway.catalog import (
    CatalogRegistry,
    advertised_input_modalities,
)
from tusker_gateway.config import load_config
from tusker_gateway.model_capability import (
    MODEL_CAPABILITY_PROBE_VERSION,
    ModelCapabilityDB,
    default_model_capability_db_path,
)
from tusker_gateway.pools import PoolManager, is_general_chat_model

logger = logging.getLogger(__name__)

_TINY_IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_TINY_WAV_BASE64 = (
    "UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAAA"
)
_MODALITY_TO_CAPABILITY = {
    "text": "input_text",
    "image": "input_image",
    "audio": "input_audio",
    "video": "input_video",
}


def _classify_http_failure(status: int, body: str) -> tuple[str, str]:
    """Classify a failure without retaining the upstream response body."""
    lowered = body.lower()
    if status in {401, 403} or any(
        marker in lowered
        for marker in ("unauthorized", "forbidden", "invalid api key", "authentication")
    ):
        return "unavailable", "auth"
    if status == 429 or any(
        marker in lowered
        for marker in ("rate limit", "rate-limited", "quota", "capacity", "resourceexhausted")
    ):
        return "unavailable", "rate_limited"
    if status >= 500:
        return "unavailable", "upstream_error"
    if status == 400 and any(
        marker in lowered
        for marker in (
            "unsupported",
            "not support",
            "image",
            "audio",
            "video",
            "modality",
            "content type",
        )
    ):
        return "unsupported", "modality_rejected"
    return "unavailable", "gateway_error"


def _messages_for_modality(modality: str) -> list[dict[str, Any]]:
    """Build the smallest OpenAI-compatible input for one modality."""
    if modality == "text":
        return [{"role": "user", "content": "Reply with the word acknowledged."}]
    content: list[dict[str, Any]] = [
        {"type": "text", "text": "Describe the supplied media in one word."},
    ]
    if modality == "image":
        content.append({
            "type": "image_url",
            "image_url": {"url": _TINY_IMAGE_DATA_URL, "detail": "low"},
        })
    elif modality == "audio":
        content.append({
            "type": "input_audio",
            "input_audio": {"data": _TINY_WAV_BASE64, "format": "wav"},
        })
    elif modality == "video":
        # Video input is provider-specific. This intentionally uses the
        # standard URL block so a provider can explicitly accept or reject it.
        content.append({
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,AAAA"},
        })
    else:
        raise ValueError(f"unsupported input modality: {modality}")
    return [{"role": "user", "content": content}]


async def probe_input_model(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    api_key: str,
    provider: str,
    model: str,
    modality: str,
    timeout_secs: float = 45.0,
) -> dict[str, Any]:
    """Send one bounded non-streaming input-modality probe."""
    started = time.monotonic()
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": f"{provider}::{model}",
        "messages": _messages_for_modality(modality),
        "stream": False,
        "temperature": 0,
        "max_tokens": 16,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Tusker-Cache": "bypass",
        "X-Tusker-Modality-Qualification": MODEL_CAPABILITY_PROBE_VERSION,
    }
    capability = _MODALITY_TO_CAPABILITY[modality]
    result: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "capability": capability,
        "status": "unavailable",
        "source": "modality_probe",
        "http_status": None,
        "latency_ms": None,
        "failure_class": None,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_secs)
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as response:
            result["http_status"] = response.status
            if response.status != 200:
                body = (await response.read())[:4096].decode("utf-8", "replace")
                result["status"], result["failure_class"] = _classify_http_failure(
                    response.status, body
                )
                return _finish_result(result, started)
            try:
                body = await response.json()
            except (TypeError, ValueError, json.JSONDecodeError):
                result["status"] = "unsupported"
                result["failure_class"] = "invalid_response"
                return _finish_result(result, started)
            if isinstance(body, dict) and (
                isinstance(body.get("choices"), list)
                or isinstance(body.get("output"), list)
            ):
                result["status"] = "passed"
            else:
                result["status"] = "unsupported"
                result["failure_class"] = "invalid_response"
    except asyncio.TimeoutError:
        result["failure_class"] = "timeout"
    except (aiohttp.ClientError, OSError) as exc:
        result["failure_class"] = type(exc).__name__
    return _finish_result(result, started)


def _finish_result(result: dict[str, Any], started: float) -> dict[str, Any]:
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def _catalog_registry(
    config: dict[str, Any],
    capability_db: ModelCapabilityDB,
    session: aiohttp.ClientSession,
) -> CatalogRegistry:
    """Build catalog clients with the same auth isolation as the gateway."""
    registry = CatalogRegistry.default(
        config.get("providers"),
        model_capability_db=capability_db,
    )
    keys = config.get("provider_api_keys", {})
    if not isinstance(keys, dict):
        keys = {}
    for provider, client in registry._clients.items():
        client.set_api_key(keys.get(provider))

    credential_pools = config.get("credential_pools", {})
    if isinstance(credential_pools, dict):
        from tusker_gateway.passthrough import CodexTokenRotator

        for provider in (
            "openai-codex",
            "github-copilot",
            "github-copilot-enterprise",
        ):
            client = registry.get_client(provider)
            credentials = credential_pools.get(provider)
            if client is None or not isinstance(credentials, list) or not credentials:
                continue
            rotator = CodexTokenRotator(
                credentials,
                http_client=session,
                provider=provider,
            )
            client.set_token_source(rotator.get_token)
    return registry


def _candidate_pairs(
    manager: PoolManager,
    registry: CatalogRegistry,
    pool_names: list[str],
    *,
    modality: str,
    include_unadvertised: bool,
    providers: set[str],
    model_pairs: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for pool_name in pool_names:
        for spec in manager.models.get(pool_name, []):
            if not is_general_chat_model(spec.provider, spec.model):
                continue
            pair = (spec.provider, spec.model)
            if providers and pair[0] not in providers:
                continue
            if model_pairs and pair not in model_pairs:
                continue
            if not include_unadvertised and modality != "text":
                entry = manager._catalog_entry_for(spec)
                advertised = spec.input_modalities
                if advertised is None:
                    advertised = advertised_input_modalities(entry)
                if not advertised or modality not in advertised:
                    continue
            pairs.add(pair)
    return sorted(pairs)


def _needs_probe(
    record: Any,
    *,
    force: bool,
    max_age_secs: float,
) -> bool:
    if force or record is None:
        return True
    if record.source != "modality_probe":
        return True
    if record.probe_version != MODEL_CAPABILITY_PROBE_VERSION:
        return True
    return (time.time() - record.checked_at) >= max_age_secs


def _route_is_quarantined(
    provider: str,
    model: str,
    cooldown_store: Any | None,
) -> bool:
    """Avoid probing a route while its provider/model quarantine is active."""
    try:
        from tusker_gateway.cooldown import global_tracker

        if global_tracker().is_cooldown(provider, model):
            return True
    except Exception:
        logger.debug("in-memory cooldown check failed", exc_info=True)
    if cooldown_store is None:
        return False
    try:
        return bool(
            cooldown_store.is_active(provider, model)
            or cooldown_store.is_provider_active(provider)
        )
    except Exception:
        logger.debug("persistent cooldown check failed", exc_info=True)
        return False


async def run_qualification(
    *,
    pool_names: list[str] | None = None,
    base_url: str = "http://127.0.0.1:8642",
    input_modality: str = "image",
    max_concurrency: int = 1,
    timeout_secs: float = 45.0,
    max_age_secs: float = 86_400.0,
    force: bool = False,
    limit: int | None = None,
    providers: set[str] | None = None,
    model_pairs: set[tuple[str, str]] | None = None,
    include_unadvertised: bool = False,
    ignore_cooldowns: bool = False,
) -> list[dict[str, Any]]:
    """Qualify one input modality across selected pool candidates."""
    config = load_config()
    gateway_key = os.environ.get("API_KEYS", "").split(",", 1)[0].strip()
    if not gateway_key:
        raise RuntimeError("API_KEYS must contain the gateway caller key")
    quality_path = config.get("quality_db_path", "data/quality.db")
    capability_db = ModelCapabilityDB(
        config.get("model_capability_db_path")
        or default_model_capability_db_path(quality_path)
    )
    cooldown_store = None
    if not ignore_cooldowns and quality_path != ":memory:":
        from tusker_gateway.persistent_cooldown import PersistentCooldownStore

        cooldown_store = PersistentCooldownStore(
            Path(quality_path).parent / "cooldowns.db"
        )
    selected_pools = pool_names or ["code"]
    provider_filter = {
        str(provider).strip().lower().replace("_", "-")
        for provider in (providers or set())
        if str(provider).strip()
    }
    model_filter = {
        (
            str(provider).strip().lower().replace("_", "-"),
            str(model).strip(),
        )
        for provider, model in (model_pairs or set())
        if str(provider).strip() and str(model).strip()
    }
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_secs)
    ) as session:
        registry = _catalog_registry(config, capability_db, session)
        await registry.refresh_all(session)
        manager = PoolManager(config)
        manager.catalog_registry = registry
        manager.extend_pools_with_free_catalog()
        pairs = _candidate_pairs(
            manager,
            registry,
            selected_pools,
            modality=input_modality,
            include_unadvertised=include_unadvertised,
            providers=provider_filter,
            model_pairs=model_filter,
        )
        capability = _MODALITY_TO_CAPABILITY[input_modality]
        pairs = [
            pair
            for pair in pairs
            if _needs_probe(
                capability_db.get(pair[0], pair[1], capability),
                force=force,
                max_age_secs=max_age_secs,
            )
        ]
        skipped_quarantine = 0
        if not ignore_cooldowns:
            unquarantined = []
            for pair in pairs:
                if _route_is_quarantined(pair[0], pair[1], cooldown_store):
                    skipped_quarantine += 1
                    continue
                unquarantined.append(pair)
            pairs = unquarantined
        if limit is not None:
            pairs = pairs[: max(0, limit)]
        logger.info(
            "modality qualification pools=%s modality=%s candidates=%d "
            "concurrency=%d include_unadvertised=%s",
            ",".join(selected_pools),
            input_modality,
            len(pairs),
            max(1, max_concurrency),
            include_unadvertised,
        )
        if skipped_quarantine:
            logger.info(
                "modality qualification modality=%s skipped_quarantined=%d",
                input_modality,
                skipped_quarantine,
            )
        semaphore = asyncio.Semaphore(max(1, max_concurrency))

        async def one(pair: tuple[str, str]) -> dict[str, Any]:
            async with semaphore:
                result = await probe_input_model(
                    session,
                    base_url=base_url,
                    api_key=gateway_key,
                    provider=pair[0],
                    model=pair[1],
                    modality=input_modality,
                    timeout_secs=timeout_secs,
                )
                capability_db.record(
                    provider=pair[0],
                    model=pair[1],
                    capability=result["capability"],
                    status=result["status"],
                    source="modality_probe",
                    probe_version=MODEL_CAPABILITY_PROBE_VERSION,
                    http_status=result.get("http_status"),
                    latency_ms=result.get("latency_ms"),
                    failure_class=result.get("failure_class"),
                )
                return result

        return await asyncio.gather(*(one(pair) for pair in pairs))


def _print_results(results: list[dict[str, Any]], *, modality: str) -> None:
    counts: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1
        latency = result.get("latency_ms")
        latency_text = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "-"
        print(
            f"{result['provider']}/{result['model']}"
            f" modality={modality} status={status}"
            f" http={result.get('http_status') or '-'}"
            f" latency={latency_text} failure={result.get('failure_class') or '-'}"
        )
    print(
        f"modality={modality} tested={len(results)} "
        f"statuses={json.dumps(counts, sort_keys=True)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pool",
        action="append",
        default=None,
        help="pool to test; repeat for multiple pools (default: code)",
    )
    parser.add_argument(
        "--all-pools",
        action="store_true",
        help="test candidates from every configured pool",
    )
    parser.add_argument(
        "--input-modality",
        choices=sorted(_MODALITY_TO_CAPABILITY),
        default="image",
    )
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="PROVIDER:MODEL",
        help="restrict to a provider:model pair; repeatable",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8642")
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--timeout-secs", type=float, default=45.0)
    parser.add_argument("--max-age-secs", type=float, default=86_400.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--ignore-cooldowns",
        action="store_true",
        help="probe quarantined routes explicitly (operator recovery test)",
    )
    parser.add_argument(
        "--include-unadvertised",
        action="store_true",
        help="probe models without catalog evidence for this modality",
    )
    args = parser.parse_args(argv)

    model_pairs: set[tuple[str, str]] = set()
    for value in args.model:
        provider, separator, model = value.partition(":")
        if not separator or not provider.strip() or not model.strip():
            parser.error("--model must be PROVIDER:MODEL")
        model_pairs.add((provider.strip(), model.strip()))

    config = load_config()
    pools = list(args.pool or ["code"])
    if args.all_pools:
        pools = list(config.get("pools", {}))
    results = asyncio.run(
        run_qualification(
            pool_names=pools,
            base_url=args.base_url,
            input_modality=args.input_modality,
            max_concurrency=args.max_concurrency,
            timeout_secs=args.timeout_secs,
            max_age_secs=args.max_age_secs,
            force=args.force,
            limit=args.limit,
            providers=set(args.provider),
            model_pairs=model_pairs,
            include_unadvertised=args.include_unadvertised,
            ignore_cooldowns=args.ignore_cooldowns,
        )
    )
    _print_results(results, modality=args.input_modality)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
