"""Qualify privacy-pool models for Hindsight's structured-output contract.

Hindsight does not merely need a successful chat response: retention and
consolidation ask for JSON.  This bounded probe sends a harmless JSON-schema
request through the gateway, stores only the result in ``ModelCapabilityDB``,
and never retains the prompt or response body.

The probe is intentionally separate from tool qualification.  A model can
support function calling and still fail Hindsight's JSON response contract.
"""
from __future__ import annotations

from copy import copy, deepcopy
import threading
import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

from tusker_gateway.cooldown import global_tracker, is_account_quota_exhausted
from tusker_gateway.config import load_config
from tusker_gateway.model_capability import (
    STRUCTURED_OUTPUT_CAPABILITY,
    STRUCTURED_OUTPUT_PROBE_VERSION,
    ModelCapabilityDB,
)
from tusker_gateway.persistent_cooldown import PersistentCooldownStore
from tusker_gateway.pools import PoolManager, is_general_chat_model

logger = logging.getLogger(__name__)

_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _probe_schema_matches(parsed: Any) -> bool:
    """Return whether parsed JSON matches the exact structured-output schema.

    The probe contract requires a JSON object with exactly one ``ok`` key
    whose value is the boolean ``true``.  Python's ``==`` considers ``1``
    equal to ``True``, so a numeric ``1`` must be rejected explicitly.
    """
    return (
        isinstance(parsed, dict)
        and set(parsed.keys()) == {"ok"}
        and parsed.get("ok") is True
    )


def _classify_http_failure(
    status: int,
    body: str,
    headers: Any | None = None,
) -> tuple[str, str]:
    """Classify a failure without retaining upstream response data."""
    lowered = body.lower()
    if status in {401, 403} or any(
        marker in lowered
        for marker in ("unauthorized", "forbidden", "invalid api key", "authentication")
    ):
        return "unavailable", "auth"
    if status == 402 or is_account_quota_exhausted(lowered):
        return "unavailable", "provider_quota"
    if status == 400 and any(
        marker in lowered
        for marker in ("response_format", "json schema", "structured output", "unsupported")
    ):
        return "unsupported", "structured_output_rejected"
    if status == 429 or any(
        marker in lowered
        for marker in ("rate limit", "rate-limited", "quota", "capacity", "temporarily")
    ):
        return "unavailable", "rate_limited"
    provider_failure = ""
    if headers is not None:
        try:
            provider_failure = str(headers.get("X-Tusker-Provider-Failure", "")).strip()
        except AttributeError:
            provider_failure = ""
    if provider_failure.lower() == "provider_quota":
        return "unavailable", "provider_quota"
    if status >= 500:
        return "unavailable", "upstream_error"
    return "unavailable", "gateway_error"


def _message_content(body: Any) -> str | None:
    """Extract text content from a Chat Completions response."""
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(item.get("text"))
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "".join(parts) or None
    return None


def _finish(result: dict[str, Any], started: float) -> dict[str, Any]:
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


async def probe_model(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    api_key: str,
    provider: str,
    model: str,
    timeout_secs: float = 45.0,
) -> dict[str, Any]:
    """Send one bounded non-streaming structured-output probe."""
    started = time.monotonic()
    result: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "capability": STRUCTURED_OUTPUT_CAPABILITY,
        "probe_version": STRUCTURED_OUTPUT_PROBE_VERSION,
        "status": "unavailable",
        "source": "structured_probe",
        "http_status": None,
        "latency_ms": None,
        "failure_class": None,
    }
    payload = {
        "model": f"{provider}::{model}",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Return only the requested JSON object. Do not emit markdown, "
                    "reasoning, or explanation."
                ),
            },
            {
                "role": "user",
                "content": "Return exactly {\"ok\":true}.",
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "tusker_structured_output_probe",
                "strict": True,
                "schema": _PROBE_SCHEMA,
            },
        },
        "stream": False,
        "temperature": 0,
        # Reasoning models may consume output tokens before their final JSON.
        "max_tokens": 512,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Tusker-Cache": "bypass",
        "X-Tusker-Structured-Qualification": STRUCTURED_OUTPUT_PROBE_VERSION,
    }
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_secs)
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as response:
            result["http_status"] = response.status
            if response.status != 200:
                body = (await response.read())[:4096].decode("utf-8", "replace")
                result["status"], result["failure_class"] = _classify_http_failure(
                    response.status,
                    body,
                    response.headers,
                )
                return _finish(result, started)
            try:
                response_body = await response.json(content_type=None)
            except (TypeError, ValueError, json.JSONDecodeError):
                result["status"] = "unsupported"
                result["failure_class"] = "invalid_response"
                return _finish(result, started)
            content = _message_content(response_body)
            if not content:
                result["status"] = "unsupported"
                result["failure_class"] = "empty_content"
                return _finish(result, started)
            try:
                parsed = json.loads(content)
            except (TypeError, ValueError, json.JSONDecodeError):
                result["status"] = "unsupported"
                result["failure_class"] = "invalid_json"
                return _finish(result, started)
            if not _probe_schema_matches(parsed):
                result["status"] = "unsupported"
                result["failure_class"] = "schema_mismatch"
                return _finish(result, started)
            result["status"] = "passed"
            return _finish(result, started)
    except asyncio.TimeoutError:
        result["failure_class"] = "timeout"
    except (aiohttp.ClientError, OSError) as exc:
        logger.debug(
            "structured qualification transport failure for %s/%s: %s",
            provider,
            model,
            type(exc).__name__,
        )
        result["failure_class"] = type(exc).__name__
    return _finish(result, started)


def _needs_probe(
    record: Any,
    *,
    force: bool,
    max_age_secs: float,
) -> bool:
    if force or record is None:
        return True
    if record.probe_version != STRUCTURED_OUTPUT_PROBE_VERSION:
        return True
    if record.status == "unavailable":
        try:
            retry_after = max(
                60.0,
                float(os.environ.get(
                    "TUSKER_STRUCTURED_QUALIFICATION_UNAVAILABLE_RETRY_SECS",
                    "900",
                )),
            )
        except (TypeError, ValueError):
            retry_after = 900.0
        return (time.time() - record.checked_at) >= retry_after
    return (time.time() - record.checked_at) >= max_age_secs


def _route_is_quarantined(
    provider: str,
    model: str,
    cooldown_store: PersistentCooldownStore | None,
) -> bool:
    """Avoid sending maintenance probes into an active quarantine."""
    try:
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


def _probe_priority(record: Any, pair: tuple[str, str]) -> tuple[Any, ...]:
    """Probe never-seen candidates first, then oldest evidence, stably."""
    return (record is not None, record.checked_at if record else 0.0, pair)



def _qualified_pairs(
    manager: PoolManager,
    capability_db: ModelCapabilityDB,
    pool_name: str,
    *,
    max_age_secs: float,
) -> list[tuple[str, str]]:
    """Enumerate fresh passes through the real selector without rotating traffic."""
    selector = copy(manager)
    selector._round_robin = {}
    selector._stickiness = {}
    selector._stickiness_expires = {}
    selector._selection_lock = threading.Lock()
    selector._cooldowns = deepcopy(manager._cooldowns)
    quality_path = str(manager.config.get("quality_db_path", "data/quality.db"))
    if quality_path != ":memory:":
        store = PersistentCooldownStore(Path(quality_path).parent / "cooldowns.db")
        store.hydrate(selector._cooldowns)
        store.hydrate_providers(selector._cooldowns)
        store.hydrate_groups(selector._cooldowns)
    now = time.time()
    fresh = {
        (record.provider, record.model)
        for record in capability_db.records()
        if record.capability == STRUCTURED_OUTPUT_CAPABILITY
        and record.status == "passed"
        and record.probe_version == STRUCTURED_OUTPUT_PROBE_VERSION
        and now - record.checked_at <= max_age_secs
    }
    excluded = {
        (spec.provider, spec.model)
        for spec in manager.models.get(pool_name, [])
        if (spec.provider, spec.model) not in fresh
    }
    qualified = []
    while pair := selector.select(
        pool_name, requires_structured_output=True, excluded=excluded, session_id=None
    ):
        qualified.append(pair)
        excluded.add(pair)
    return sorted(qualified)


def qualified_count(
    *,
    manager: PoolManager,
    pool_name: str = "privacy",
    max_age_secs: float = 21_600.0,
) -> int:
    """Count selectable fresh passes from the qualification run's pool snapshot."""
    return len(_qualified_pairs(
        manager, manager._model_capability_db, pool_name, max_age_secs=max_age_secs,
    ))


async def run_structured_qualification(
    *,
    pool_name: str = "privacy",
    base_url: str = "http://127.0.0.1:8642",
    max_concurrency: int = 1,
    timeout_secs: float = 45.0,
    max_age_secs: float = 21_600.0,
    force: bool = False,
    limit: int | None = None,
    providers: set[str] | None = None,
    model_pairs: set[tuple[str, str]] | None = None,
    ignore_cooldowns: bool = False,
    catalog_registry: Any | None = None,
    manager: PoolManager | None = None,
) -> list[dict[str, Any]]:
    """Probe configured general-chat candidates in one pool.

    The catalog registry is refreshed once (or reused when ``catalog_registry``
    is supplied) and merged into the pool so auto-discovered catalog entries
    are probed, matching the tool-qualification runner.
    """
    config = manager.config if manager is not None else load_config()
    api_key = os.environ.get("API_KEYS", "").split(",", 1)[0].strip()
    if not api_key:
        raise RuntimeError("API_KEYS must contain the gateway caller key")
    quality_path = str(config.get("quality_db_path", "data/quality.db"))
    if manager is None:
        manager = PoolManager(config)
    capability_db = manager._model_capability_db
    timeout = aiohttp.ClientTimeout(total=timeout_secs, sock_read=timeout_secs)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        registry = catalog_registry
        if registry is None:
            from tusker_gateway.tool_qualification import _catalog_registry

            registry = _catalog_registry(config, http_client=session)
            await registry.refresh_all(session)
        manager.catalog_registry = registry
        manager.extend_pools_with_free_catalog()
        pairs = sorted(
            {
                (spec.provider, spec.model)
                for spec in manager.models.get(pool_name, [])
                if is_general_chat_model(spec.provider, spec.model)
            }
        )
        provider_filter = {
            str(provider).strip().lower().replace("_", "-")
            for provider in (providers or set())
            if str(provider).strip()
        }
        if provider_filter:
            pairs = [pair for pair in pairs if pair[0] in provider_filter]
        model_filter = {
            (
                str(provider).strip().lower().replace("_", "-"),
                str(model),
            )
            for provider, model in (model_pairs or set())
            if str(provider).strip() and str(model).strip()
        }
        if model_filter:
            pairs = [pair for pair in pairs if pair in model_filter]
        records = {
            (record.provider, record.model): record
            for record in capability_db.records()
            if record.capability == STRUCTURED_OUTPUT_CAPABILITY
        }
        pairs = [
            pair
            for pair in pairs
            if _needs_probe(
                records.get(pair),
                force=force,
                max_age_secs=max_age_secs,
            )
        ]
        cooldown_store: PersistentCooldownStore | None = None
        if not ignore_cooldowns and quality_path != ":memory:":
            cooldown_store = PersistentCooldownStore(Path(quality_path).parent / "cooldowns.db")
        if not ignore_cooldowns:
            pairs = [
                pair
                for pair in pairs
                if not _route_is_quarantined(pair[0], pair[1], cooldown_store)
            ]
        # Cycle through unseen candidates before revisiting the oldest evidence.
        pairs.sort(key=lambda pair: _probe_priority(records.get(pair), pair))
        if limit is not None:
            pairs = pairs[: max(0, limit)]
        logger.info(
            "structured qualification pool=%s candidates=%d concurrency=%d",
            pool_name,
            len(pairs),
            max(1, max_concurrency),
        )
        semaphore = asyncio.Semaphore(max(1, max_concurrency))

        async def one(pair: tuple[str, str]) -> dict[str, Any]:
            async with semaphore:
                result = await probe_model(
                    session,
                    base_url=base_url,
                    api_key=api_key,
                    provider=pair[0],
                    model=pair[1],
                    timeout_secs=timeout_secs,
                )
                capability_db.record(**result)
                return result

        return await asyncio.gather(*(one(pair) for pair in pairs))


def _print_results(results: list[dict[str, Any]], *, pool_name: str) -> None:
    counts: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1
        latency = result.get("latency_ms")
        latency_text = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "-"
        print(
            f"{result['provider']}/{result['model']}"
            f" status={status} http={result.get('http_status') or '-'}"
            f" latency={latency_text} failure={result.get('failure_class') or '-'}"
        )
    print(f"pool={pool_name} tested={len(results)} statuses={json.dumps(counts, sort_keys=True)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", default="privacy")
    parser.add_argument(
        "--model",
        action="append",
        metavar="PROVIDER/MODEL",
        help="limit the probe to an exact provider/model pair; repeat as needed",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("TUSKER_STRUCTURED_QUALIFICATION_BASE_URL", "http://127.0.0.1:8642"),
    )
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--timeout-secs", type=float, default=45.0)
    parser.add_argument("--max-age-secs", type=float, default=21_600.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ignore-cooldowns", action="store_true")
    args = parser.parse_args(argv)
    model_pairs: set[tuple[str, str]] = set()
    for value in args.model or []:
        provider, separator, model = value.partition("/")
        if not separator or not provider.strip() or not model.strip():
            parser.error(f"--model must be PROVIDER/MODEL, got {value!r}")
        model_pairs.add((provider.strip(), model.strip()))
    results = asyncio.run(
        run_structured_qualification(
            pool_name=args.pool,
            base_url=args.base_url,
            max_concurrency=args.max_concurrency,
            timeout_secs=args.timeout_secs,
            max_age_secs=args.max_age_secs,
            force=args.force,
            limit=args.limit,
            model_pairs=model_pairs or None,
            ignore_cooldowns=args.ignore_cooldowns,
        )
    )
    _print_results(results, pool_name=args.pool)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
