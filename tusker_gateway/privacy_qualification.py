"""Privacy-pool route qualification.

A privacy request is only safe when the concrete upstream route accepted the
gateway's privacy request policy — for Command Code that is the
``x-cmd-zdr: 1`` header, applied by ``PassthroughClient._build_request``
when ``pool="privacy"``. Ordinary modality/tool probes address
``provider::model`` passthrough routes, so they never exercise that policy
and cannot prove it. This module sends one bounded non-streaming chat
request directly through the same post-selection dispatch path a live
privacy request takes (``PassthroughClient.chat(..., pool="privacy")``) and
persists the result as ``privacy_zdr`` capability evidence.

``PoolManager`` consumes that evidence to hold auto-discovered privacy
candidates until a route has answered *as a privacy route*.

Usage::

    python -m tusker_gateway.privacy_qualification \
        --model commandcode:deepseek/deepseek-v4-flash
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from tusker_gateway.config import load_config
from tusker_gateway.model_capability import (
    PRIVACY_ZDR_CAPABILITY,
    PRIVACY_ZDR_PROBE_VERSION,
    ModelCapabilityDB,
    default_model_capability_db_path,
)

logger = logging.getLogger(__name__)


async def probe_privacy_model(
    config: dict[str, Any],
    http_client: aiohttp.ClientSession,
    *,
    provider: str,
    model: str,
    timeout_secs: float = 45.0,
) -> dict[str, Any]:
    """Qualify one concrete route through the privacy request path.

    The probe bypasses pool selection on purpose: selection cannot pin a
    specific pair, so a pool-wide request could be answered by a different
    already-qualified candidate and would prove nothing about *this* route.
    Calling ``client.chat`` with ``pool="privacy"`` still exercises the
    complete privacy request contract — auth strategy, provider request
    policy (including Command Code's ``x-cmd-zdr``), response validation,
    and cooldown bookkeeping — which is exactly the path a selected
    privacy candidate takes after selection.
    """
    from tusker_gateway.passthrough import PassthroughClient
    from tusker_gateway.quality import QualityDB

    quality_db = QualityDB(config.get("quality_db_path", "data/quality.db"))
    client = PassthroughClient(config, quality_db, http_client)
    messages = [
        {
            "role": "user",
            "content": "Reply with the word acknowledged.",
        }
    ]
    result: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "capability": PRIVACY_ZDR_CAPABILITY,
        "status": "unavailable",
        "source": "privacy_probe",
        "http_status": None,
        "latency_ms": None,
        "failure_class": None,
    }
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            client.chat(
                provider,
                model,
                messages,
                stream=False,
                pool="privacy",
            ),
            timeout=timeout_secs,
        )
    except asyncio.TimeoutError:
        result["failure_class"] = "timeout"
        return _finish(result, started)
    except Exception as exc:
        # Provider errors carry the upstream status; anything else is a
        # transport or gateway failure and reports only the failure class.
        result["http_status"] = getattr(exc, "upstream_status", None)
        result["failure_class"] = str(
            getattr(exc, "code", None) or type(exc).__name__
        )
        return _finish(result, started)
    choices = response.get("choices") if isinstance(response, dict) else None
    ok = isinstance(choices, list) and bool(choices)
    result["status"] = "passed" if ok else "unsupported"
    result["http_status"] = 200 if ok else None
    result["failure_class"] = None if ok else "invalid_response"
    return _finish(result, started)


def _finish(result: dict[str, Any], started: float) -> dict[str, Any]:
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


async def run_privacy_qualification(
    *,
    base_url: str = "http://127.0.0.1:8642",
    max_concurrency: int = 1,
    timeout_secs: float = 45.0,
    max_age_secs: float = 86_400.0,
    limit: int | None = None,
    model_pairs: set[tuple[str, str]] | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Probe privacy-pool candidates and persist ``privacy_zdr`` evidence.

    ``model_pairs`` restricts the run to explicit provider/model pairs;
    otherwise every auto-discovered privacy candidate that lacks fresh
    evidence is probed. Evidence records never retain request or response
    bodies.
    """
    config = load_config()
    quality_path = config.get("quality_db_path", "data/quality.db")
    capability_db = ModelCapabilityDB(
        config.get("model_capability_db_path")
        or default_model_capability_db_path(quality_path)
    )
    if model_pairs:
        pairs = sorted(
            {
                (str(provider).strip(), str(model).strip())
                for provider, model in model_pairs
                if str(provider).strip() and str(model).strip()
            }
        )
    else:
        from tusker_gateway.modality_qualification import _catalog_registry
        from tusker_gateway.pools import PoolManager

        manager = PoolManager(config)
        probe_timeout = aiohttp.ClientTimeout(total=timeout_secs)
        async with aiohttp.ClientSession(timeout=probe_timeout) as session:
            registry = _catalog_registry(config, capability_db, session)
            await registry.refresh_all(session)
        manager.catalog_registry = registry
        manager.extend_pools_with_auto_catalog()
        pairs = sorted(
            {
                (spec.provider, spec.model)
                for spec in manager.models.get("privacy", ())
                if spec.auto_discovered
            }
        )
    from tusker_gateway.tool_qualification import _interleave_probe_candidates

    if not force:
        # A bounded batch must not be spent re-probing the same alphabetically
        # first routes when their provider is quota-blocked: on 2026-11-22 the
        # eight ``commandcode/Qwen/*`` routes were re-probed every cycle and
        # returned ``unavailable`` each time, so the other ~180 auto-discovered
        # privacy routes were never examined. Treat transient ``unavailable``
        # evidence as due, matching behavioural/composite qualification.
        now = time.time()
        pairs = [
            pair
            for pair in pairs
            if (
                (record := capability_db.get(*pair, PRIVACY_ZDR_CAPABILITY)) is None
                or record.probe_version != PRIVACY_ZDR_PROBE_VERSION
                or now - record.checked_at >= max_age_secs
                or str(record.status or "").lower() == "unavailable"
            )
        ]
    # Interleave even for explicit force runs: a bounded --limit must span
    # providers rather than draining the first one alphabetically.
    pairs = _interleave_probe_candidates(pairs, capability_db, PRIVACY_ZDR_CAPABILITY)
    if limit is not None:
        pairs = pairs[: max(0, limit)]
    logger.info(
        "privacy qualification candidates=%d concurrency=%d",
        len(pairs),
        max(1, max_concurrency),
    )
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    timeout = aiohttp.ClientTimeout(total=timeout_secs, sock_read=timeout_secs)

    async def one(pair: tuple[str, str]) -> dict[str, Any]:
        async with semaphore:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                result = await probe_privacy_model(
                    config,
                    session,
                    provider=pair[0],
                    model=pair[1],
                    timeout_secs=timeout_secs,
                )
            capability_db.record(
                provider=pair[0],
                model=pair[1],
                capability=PRIVACY_ZDR_CAPABILITY,
                status=result["status"],
                source="privacy_probe",
                probe_version=PRIVACY_ZDR_PROBE_VERSION,
                http_status=result.get("http_status"),
                latency_ms=result.get("latency_ms"),
                failure_class=result.get("failure_class"),
            )
            return result

    return await asyncio.gather(*(one(pair) for pair in pairs))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args(argv)

    model_pairs: set[tuple[str, str]] = set()
    for value in args.model:
        provider, separator, model = value.partition(":")
        if not separator or not provider.strip() or not model.strip():
            parser.error("--model must use PROVIDER:MODEL syntax")
        model_pairs.add((provider.strip(), model.strip()))

    results = asyncio.run(
        run_privacy_qualification(
            base_url=args.base_url,
            max_concurrency=args.max_concurrency,
            timeout_secs=args.timeout_secs,
            max_age_secs=args.max_age_secs,
            limit=args.limit,
            model_pairs=model_pairs,
            force=args.force,
        )
    )
    print(json.dumps(results, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
