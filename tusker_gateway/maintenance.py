"""Low-rate runtime maintenance for provider capability evidence.

The gateway keeps qualification separate from request routing. This module
only schedules small batches of the existing tool probes, rotates through the
configured pools, and purges expired persistent cooldown rows. It never
probes media-generation endpoints.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from tusker_gateway.config import load_config
from tusker_gateway.persistent_cooldown import PersistentCooldownStore
from tusker_gateway.tool_qualification import run_qualification

logger = logging.getLogger(__name__)

_DEFAULT_POOL_ORDER = ("code", "privacy", "premium", "swarm")


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _maintenance_pools(config: dict[str, Any]) -> tuple[str, ...]:
    """Return a stable, operator-overridable pool rotation order."""
    configured = {
        str(name).strip().lower().replace("_", "-")
        for name in config.get("pools", {})
        if str(name).strip()
    }
    requested = tuple(
        item.strip().lower().replace("_", "-")
        for item in os.environ.get("TUSKER_QUALIFICATION_MAINTENANCE_POOLS", "").split(",")
        if item.strip()
    )
    order = requested or _DEFAULT_POOL_ORDER
    result = [name for name in order if name in configured]
    result.extend(name for name in sorted(configured) if name not in result)
    return tuple(result)


def _cooldown_store(config: dict[str, Any]) -> PersistentCooldownStore | None:
    quality_path = str(config.get("quality_db_path", "data/quality.db"))
    if quality_path == ":memory:":
        return None
    return PersistentCooldownStore(Path(quality_path).parent / "cooldowns.db")


async def _wait_or_stop(stop_event: asyncio.Event, delay_secs: float) -> bool:
    """Wait for a delay and return whether shutdown was requested."""
    if delay_secs <= 0:
        return stop_event.is_set()
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_secs)
    except asyncio.TimeoutError:
        return False
    return True


async def run_maintenance_cycle(
    *,
    pool_name: str,
    base_url: str = "http://127.0.0.1:8642",
    limit: int = 12,
    timeout_secs: float = 30.0,
    max_age_secs: float = 86_400.0,
) -> dict[str, Any]:
    """Run one bounded tool-qualification batch and purge expired cooldowns."""
    config = load_config()
    store = _cooldown_store(config)
    purged = store.purge_expired() if store is not None else 0
    results = await run_qualification(
        pool_name=pool_name,
        base_url=base_url,
        max_concurrency=1,
        timeout_secs=timeout_secs,
        max_age_secs=max_age_secs,
        limit=limit,
        # The qualification runner skips active provider/model quarantines by
        # default, so maintenance cannot turn a known outage into a retry storm.
        ignore_cooldowns=False,
    )
    passed = sum(1 for result in results if result.get("status") == "passed")
    return {
        "pool": pool_name,
        "tested": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "purged_cooldowns": purged,
    }


async def qualification_maintenance_loop(stop_event: asyncio.Event) -> None:
    """Rotate small qualification batches without blocking gateway startup."""
    config = load_config()
    pools = _maintenance_pools(config)
    if not pools:
        logger.warning("qualification maintenance disabled: no configured pools")
        return

    interval_secs = _env_float(
        "TUSKER_QUALIFICATION_MAINTENANCE_INTERVAL_SECS",
        21_600.0,
        minimum=60.0,
    )
    initial_delay_secs = _env_float(
        "TUSKER_QUALIFICATION_MAINTENANCE_INITIAL_DELAY_SECS",
        300.0,
        minimum=0.0,
    )
    limit = _env_int(
        "TUSKER_QUALIFICATION_MAINTENANCE_LIMIT",
        12,
        minimum=1,
    )
    timeout_secs = _env_float(
        "TUSKER_QUALIFICATION_MAINTENANCE_TIMEOUT_SECS",
        30.0,
        minimum=5.0,
    )
    max_age_secs = _env_float(
        "TUSKER_QUALIFICATION_MAINTENANCE_MAX_AGE_SECS",
        86_400.0,
        minimum=60.0,
    )
    base_url = os.environ.get(
        "TUSKER_TOOL_QUALIFICATION_BASE_URL",
        "http://127.0.0.1:8642",
    ).strip() or "http://127.0.0.1:8642"

    logger.info(
        "qualification maintenance started interval=%.0fs initial_delay=%.0fs "
        "limit=%d pools=%s",
        interval_secs,
        initial_delay_secs,
        limit,
        ",".join(pools),
    )
    first_cycle = True
    pool_index = 0
    while not stop_event.is_set():
        delay = initial_delay_secs if first_cycle else interval_secs
        first_cycle = False
        if await _wait_or_stop(stop_event, delay):
            return
        pool_name = pools[pool_index % len(pools)]
        pool_index += 1
        try:
            summary = await run_maintenance_cycle(
                pool_name=pool_name,
                base_url=base_url,
                limit=limit,
                timeout_secs=timeout_secs,
                max_age_secs=max_age_secs,
            )
            logger.info("qualification maintenance result=%s", summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep the scheduler alive and avoid logging provider response data.
            logger.warning(
                "qualification maintenance cycle failed pool=%s error_class=%s",
                pool_name,
                type(exc).__name__,
            )
