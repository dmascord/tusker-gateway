"""Qualify auto-discovered models against the gateway tool-call contract.

The command is intentionally a separate, low-concurrency operation. It sends
one harmless fictional tool request per selected model through the gateway's
public boundary and stores only the behavioral result, never the response
body or any credential.

Usage::

    python -m tusker_gateway.tool_qualification --pool code
    tusker-gateway-qualify-tools --pool code --max-concurrency 1
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

from tusker_gateway.catalog import CatalogRegistry
from tusker_gateway.config import load_config
from tusker_gateway.pools import PoolManager, is_general_chat_model
from tusker_gateway.tool_capability import (
    TOOL_CAPABILITY_PROBE_VERSION,
    ToolCapabilityDB,
    ToolCapabilityLevel,
    default_tool_capability_db_path,
)

logger = logging.getLogger(__name__)

PROBE_TOOL_NAME = "tusker_capability_probe"
PROBE_PATH = "/tmp/tusker-tool-capability-probe"

PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PROBE_TOOL_NAME,
        "description": "Return a fixed harmless result. Never execute commands.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


def _catalog_registry(
    config: dict[str, Any],
    *,
    http_client: aiohttp.ClientSession | None = None,
) -> CatalogRegistry:
    """Build and authenticate catalogs for every configured provider."""
    registry = CatalogRegistry.default(config.get("providers"))
    keys = config.get("provider_api_keys", {})
    if not isinstance(keys, dict):
        keys = {}
    for provider, client in registry._clients.items():
        client.set_api_key(keys.get(provider))

    # Codex and Copilot model catalogs use short-lived OAuth credentials. The
    # gateway process wires full rotators; the standalone qualification job
    # only needs a read-only token source for catalog enumeration.
    credential_pools = config.get("credential_pools", {})
    if isinstance(credential_pools, dict):
        from tusker_gateway.passthrough import CodexTokenRotator

        for provider in (
            "openai-codex",
            "github-copilot",
            "github-copilot-enterprise",
        ):
            client = registry.get_client(provider)
            if client is None or keys.get(provider):
                continue
            credentials = credential_pools.get(provider)
            if not isinstance(credentials, list) or not credentials:
                continue
            rotator = CodexTokenRotator(
                credentials,
                http_client=http_client,
                provider=provider,
            )
            client.set_token_source(rotator.get_token)
    return registry


def _parse_sse_line(
    line: str,
    calls: dict[int, dict[str, str]],
    text_parts: list[str],
) -> str | None:
    """Consume one SSE data line and return its terminal finish reason."""
    if not line.startswith("data:"):
        return None
    data = line[5:].lstrip()
    if not data or data == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None

    finish_reason: str | None = None
    for choice in event.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = {}
        content = delta.get("content")
        if isinstance(content, str):
            text_parts.append(content)
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for item in tool_calls:
                if not isinstance(item, dict):
                    continue
                try:
                    index = int(item.get("index", 0))
                except (TypeError, ValueError):
                    index = 0
                call = calls.setdefault(index, {"name": "", "arguments": ""})
                function = item.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                arguments = function.get("arguments")
                if isinstance(name, str):
                    call["name"] = name
                if isinstance(arguments, str):
                    call["arguments"] += arguments
        reason = choice.get("finish_reason")
        if reason is not None:
            finish_reason = str(reason)
    return finish_reason


def _classify_http_failure(status: int, body: str) -> tuple[ToolCapabilityLevel, str, str]:
    """Classify transport failures without retaining the upstream body."""
    lowered = body.lower()
    if status == 400 and any(
        marker in lowered for marker in ("tool", "function calling", "unsupported parameter")
    ):
        return ToolCapabilityLevel.UNSUPPORTED, "unsupported", "tool_request_rejected"
    if status in {401, 403} or any(
        marker in lowered for marker in ("unauthorized", "forbidden", "invalid api key")
    ):
        return ToolCapabilityLevel.UNAVAILABLE, "unavailable", "auth"
    if status == 429 or any(
        marker in lowered for marker in ("rate limit", "rate-limited", "quota", "temporarily")
    ):
        return ToolCapabilityLevel.UNAVAILABLE, "unavailable", "rate_limited"
    if status >= 500:
        return ToolCapabilityLevel.UNAVAILABLE, "unavailable", "upstream_error"
    return ToolCapabilityLevel.UNAVAILABLE, "unavailable", "gateway_error"


def _result_from_stream(
    *,
    provider: str,
    model: str,
    status_code: int,
    latency_ms: float,
    calls: dict[int, dict[str, str]],
    text_parts: list[str],
    finish_reason: str | None,
) -> dict[str, Any]:
    """Convert a parsed response into a safe DB/report record."""
    call_count = len(calls)
    arguments_valid = False
    arguments_match = False
    function_name: str | None = None
    if call_count == 1:
        call = next(iter(calls.values()))
        function_name = call.get("name") or None
        try:
            arguments = json.loads(call.get("arguments", ""))
        except (TypeError, json.JSONDecodeError):
            arguments = None
        arguments_valid = isinstance(arguments, dict)
        arguments_match = (
            function_name == PROBE_TOOL_NAME
            and arguments == {"path": PROBE_PATH}
        )

    structured = call_count > 0
    valid_tool_contract = (
        status_code == 200
        and call_count == 1
        and function_name == PROBE_TOOL_NAME
        and arguments_valid
        and arguments_match
        and finish_reason == "tool_calls"
    )
    strict = (
        valid_tool_contract
        and not "".join(text_parts).strip()
    )
    if strict:
        level = ToolCapabilityLevel.STRICT_STRUCTURED_STREAM
        status = "passed"
        failure_class = None
    elif valid_tool_contract:
        # Text alongside a structured tool call is valid OpenAI chat output.
        # Keep the distinction from the strict no-prose result for diagnostics
        # without excluding the model from normal tool-bearing routing.
        level = ToolCapabilityLevel.STRUCTURED_STREAM
        status = "passed"
        failure_class = "unexpected_text"
    elif structured:
        level = ToolCapabilityLevel.STRUCTURED_STREAM
        status = "failed"
        failure_class = "non_strict_tool_contract"
    else:
        level = ToolCapabilityLevel.UNSUPPORTED
        status = "failed"
        failure_class = "no_tool_call"

    return {
        "provider": provider,
        "model": model,
        "level": level,
        "status": status,
        "http_status": status_code,
        "tool_call_count": call_count,
        "structured_stream": structured,
        "arguments_valid": arguments_valid,
        "arguments_match": arguments_match,
        "finish_reason": finish_reason,
        "unexpected_text": bool("".join(text_parts).strip()),
        "latency_ms": round(latency_ms, 1),
        "failure_class": failure_class,
    }


async def probe_model(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    api_key: str,
    provider: str,
    model: str,
) -> dict[str, Any]:
    """Send one streaming required-tool probe to a concrete model route."""
    payload = {
        "model": f"{provider}/{model}",
        "messages": [
            {
                "role": "system",
                "content": "You are being tested for strict tool-call compliance. Do not emit prose.",
            },
            {
                "role": "user",
                "content": (
                    f"Call {PROBE_TOOL_NAME} exactly once with path {PROBE_PATH}. "
                    "Do not call any other function."
                ),
            },
        ],
        "tools": [PROBE_TOOL],
        "tool_choice": "required",
        "stream": True,
        "temperature": 0,
        "max_tokens": 192,
    }
    started = time.monotonic()
    calls: dict[int, dict[str, str]] = {}
    text_parts: list[str] = []
    finish_reason: str | None = None
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Tusker-Cache": "bypass",
        "X-Tusker-Tool-Qualification": TOOL_CAPABILITY_PROBE_VERSION,
    }
    try:
        async with session.post(url, json=payload, headers=headers) as response:
            status_code = response.status
            if status_code != 200:
                body = (await response.read()).decode("utf-8", "replace")[:4096]
                level, status, failure_class = _classify_http_failure(status_code, body)
                return {
                    "provider": provider,
                    "model": model,
                    "level": level,
                    "status": status,
                    "http_status": status_code,
                    "tool_call_count": 0,
                    "structured_stream": False,
                    "arguments_valid": False,
                    "arguments_match": False,
                    "finish_reason": None,
                    "unexpected_text": False,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "failure_class": failure_class,
                }
            async for raw_line in response.content:
                reason = _parse_sse_line(
                    raw_line.decode("utf-8", "replace").strip(),
                    calls,
                    text_parts,
                )
                if reason is not None:
                    finish_reason = reason
    except asyncio.TimeoutError:
        return {
            "provider": provider,
            "model": model,
            "level": ToolCapabilityLevel.UNAVAILABLE,
            "status": "unavailable",
            "http_status": None,
            "tool_call_count": 0,
            "structured_stream": False,
            "arguments_valid": False,
            "arguments_match": False,
            "finish_reason": None,
            "unexpected_text": False,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "failure_class": "timeout",
        }
    except (aiohttp.ClientError, OSError) as exc:
        logger.debug("qualification transport failure for %s/%s: %s", provider, model, exc)
        return {
            "provider": provider,
            "model": model,
            "level": ToolCapabilityLevel.UNAVAILABLE,
            "status": "unavailable",
            "http_status": None,
            "tool_call_count": 0,
            "structured_stream": False,
            "arguments_valid": False,
            "arguments_match": False,
            "finish_reason": None,
            "unexpected_text": False,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "failure_class": type(exc).__name__,
        }

    return _result_from_stream(
        provider=provider,
        model=model,
        status_code=200,
        latency_ms=(time.monotonic() - started) * 1000,
        calls=calls,
        text_parts=text_parts,
        finish_reason=finish_reason,
    )


def _needs_probe(
    record: Any,
    *,
    force: bool,
    max_age_secs: float,
) -> bool:
    if force or record is None:
        return True
    if record.probe_version != TOOL_CAPABILITY_PROBE_VERSION:
        return True
    if record.level == ToolCapabilityLevel.UNAVAILABLE:
        try:
            retry_after = max(
                60.0,
                float(os.environ.get(
                    "TUSKER_TOOL_QUALIFICATION_UNAVAILABLE_RETRY_SECS",
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
    cooldown_store: Any | None,
) -> bool:
    """Avoid sending maintenance probes into an active provider quarantine."""
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
    pool_name: str = "code",
    base_url: str = "http://127.0.0.1:8642",
    max_concurrency: int = 1,
    timeout_secs: float = 45.0,
    max_age_secs: float = 86_400.0,
    force: bool = False,
    limit: int | None = None,
    providers: set[str] | None = None,
    model_pairs: set[tuple[str, str]] | None = None,
    ignore_cooldowns: bool = False,
) -> list[dict[str, Any]]:
    """Qualify selected static and auto-discovered chat models for one pool."""
    config = load_config()
    api_key = os.environ.get("API_KEYS", "").split(",", 1)[0].strip()
    if not api_key:
        raise RuntimeError("API_KEYS must contain the gateway caller key")
    quality_path = config.get("quality_db_path", "data/quality.db")
    capability_db = ToolCapabilityDB(
        config.get("tool_capability_db_path")
        or default_tool_capability_db_path(quality_path)
    )
    cooldown_store = None
    if not ignore_cooldowns and quality_path != ":memory:":
        from tusker_gateway.persistent_cooldown import PersistentCooldownStore

        cooldown_store = PersistentCooldownStore(
            Path(quality_path).parent / "cooldowns.db"
        )
    timeout = aiohttp.ClientTimeout(total=timeout_secs, sock_read=timeout_secs)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        registry = _catalog_registry(config, http_client=session)
        await registry.refresh_all(session)
        manager = PoolManager(config)
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
        model_filter = {
            (
                str(provider).strip().lower().replace("_", "-"),
                str(model),
            )
            for provider, model in (model_pairs or set())
            if str(provider).strip() and str(model).strip()
        }
        if provider_filter:
            pairs = [pair for pair in pairs if pair[0] in provider_filter]
        if model_filter:
            pairs = [pair for pair in pairs if pair in model_filter]
        pairs = [
            pair for pair in pairs
            if _needs_probe(
                capability_db.get(*pair),
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
            pairs = pairs[:limit]
        logger.info(
            "tool qualification pool=%s candidates=%d concurrency=%d providers=%s models=%s",
            pool_name,
            len(pairs),
            max_concurrency,
            ",".join(sorted(provider_filter)) or "all",
            len(model_filter) or "all",
        )
        if skipped_quarantine:
            logger.info(
                "tool qualification pool=%s skipped_quarantined=%d",
                pool_name,
                skipped_quarantine,
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
                )
                capability_db.record(**result)
                return result

        return await asyncio.gather(*(one(pair) for pair in pairs))


def _print_results(results: list[dict[str, Any]], *, pool_name: str) -> None:
    counts: dict[str, int] = {}
    for result in results:
        level = ToolCapabilityLevel(result["level"]).name.lower()
        counts[level] = counts.get(level, 0) + 1
        latency = result.get("latency_ms")
        latency_text = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "-"
        failure = result.get("failure_class") or "-"
        print(
            f"{result['provider']}/{result['model']}"
            f" status={result['status']} level={level}"
            f" http={result.get('http_status') or '-'} latency={latency_text}"
            f" failure={failure}"
        )
    print(f"pool={pool_name} tested={len(results)} levels={json.dumps(counts, sort_keys=True)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pool",
        default="code",
        help="pool whose static and auto-discovered chat models should be tested",
    )
    parser.add_argument(
        "--provider",
        dest="providers",
        action="append",
        help="limit probes to this provider; repeat for multiple providers",
    )
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        metavar="PROVIDER/MODEL",
        help="limit probes to an exact provider/model pair; repeat as needed",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("TUSKER_TOOL_QUALIFICATION_BASE_URL", "http://127.0.0.1:8642"),
    )
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--timeout-secs", type=float, default=45.0)
    parser.add_argument("--max-age-secs", type=float, default=86_400.0)
    parser.add_argument("--force", action="store_true", help="retest even a fresh record")
    parser.add_argument(
        "--ignore-cooldowns",
        action="store_true",
        help="probe quarantined routes explicitly (operator recovery test)",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--json", action="store_true", help="emit result records as JSON")
    args = parser.parse_args(argv)
    model_pairs: set[tuple[str, str]] = set()
    for value in args.models or []:
        provider, separator, model = value.partition("/")
        if not separator or not provider.strip() or not model.strip():
            parser.error(f"--model must use PROVIDER/MODEL syntax: {value!r}")
        model_pairs.add((provider.strip(), model.strip()))
    logging.basicConfig(
        level=os.environ.get("TUSKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    results = asyncio.run(
        run_qualification(
            pool_name=args.pool,
            base_url=args.base_url,
            max_concurrency=args.max_concurrency,
            timeout_secs=args.timeout_secs,
            max_age_secs=args.max_age_secs,
            force=args.force,
            limit=args.limit,
            providers=set(args.providers or []),
            model_pairs=model_pairs,
            ignore_cooldowns=args.ignore_cooldowns,
        )
    )
    if args.json:
        print(json.dumps(results, default=lambda value: value.name.lower() if isinstance(value, ToolCapabilityLevel) else str(value), sort_keys=True))
    else:
        _print_results(results, pool_name=args.pool)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
