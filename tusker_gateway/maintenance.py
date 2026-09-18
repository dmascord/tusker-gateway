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
import time
from pathlib import Path
from typing import Any

from tusker_gateway.config import load_config
from tusker_gateway.persistent_cooldown import PersistentCooldownStore
from tusker_gateway.structured_qualification import (
    qualified_count,
    run_structured_qualification,
)
from tusker_gateway.tool_qualification import run_qualification

logger = logging.getLogger(__name__)

_DEFAULT_POOL_ORDER = ("code", "privacy", "premium", "swarm")
_DEFAULT_STRUCTURED_POOL = "privacy"
# Vision+tool requests are bottlenecked when the live catalog loses the
# input_modalities field for models that actually support image input (the
# pool-eligibility filter strips them before any other fallback can run).
# A periodic modality probe against the gateway boundary keeps the capability
# DB authoritative, so a stale catalog row does not lock vision-capable
# routes out of selection.
_DEFAULT_MODALITY_POOL_ORDER = ("code", "privacy", "premium", "swarm")
_DEFAULT_MODALITY = "image"


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


def _structured_qualification_enabled(config: dict[str, Any]) -> bool:
    """Return whether the Hindsight JSON contract monitor is enabled."""
    raw = os.environ.get("TUSKER_STRUCTURED_QUALIFICATION_ENABLED", "1")
    if raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return _structured_pool_name(config) in config.get("pools", {})


def _structured_pool_name(config: dict[str, Any]) -> str:
    return (
        os.environ.get(
            "TUSKER_STRUCTURED_QUALIFICATION_POOL",
            _DEFAULT_STRUCTURED_POOL,
        )
        .strip()
        .lower()
        .replace("_", "-")
    )


def _modality_qualification_enabled(config: dict[str, Any]) -> bool:
    """Return whether the input-modality probe maintenance is enabled.

    Off by default — an operator opts in once vision+tool traffic is observed
    failing the pool-eligibility filter. The probe is bounded and read-only
    (1×1px image / 1×16-byte wav) but still costs an upstream round trip per
    candidate, so the operator retains explicit control.
    """
    raw = os.environ.get("TUSKER_MODALITY_QUALIFICATION_ENABLED", "0")
    if raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    pools = _modality_pool_order(config)
    return bool(pools)


def _modality_pool_order(config: dict[str, Any]) -> tuple[str, ...]:
    """Return the operator-overridable rotation order for modality probes.

    Unlike the tool-qualification rotation, modality probes are explicitly
    scoped to ``code``/``premium``/``swarm`` by default. The ``privacy``
    pool is excluded on purpose — image-bearing requests never go there, and
    keeping the probe set small avoids spending maintenance cycles on routes
    the filter would never select. Operator overrides are honoured but
    never extended with leftovers; an unknown pool name silently drops.
    """
    configured = {
        str(name).strip().lower().replace("_", "-")
        for name in config.get("pools", {})
        if str(name).strip()
    }
    requested = tuple(
        item.strip().lower().replace("_", "-")
        for item in os.environ.get("TUSKER_MODALITY_QUALIFICATION_POOLS", "").split(",")
        if item.strip()
    )
    order = requested or _DEFAULT_MODALITY_POOL_ORDER
    return tuple(name for name in order if name in configured)


def _modality_name() -> str:
    raw = (
        os.environ.get("TUSKER_MODALITY_QUALIFICATION_MODALITY", _DEFAULT_MODALITY).strip().lower()
    )
    return raw or _DEFAULT_MODALITY


async def run_structured_maintenance_cycle(
    *,
    pool_name: str = _DEFAULT_STRUCTURED_POOL,
    base_url: str = "http://127.0.0.1:8642",
    limit: int = 4,
    timeout_secs: float = 45.0,
    max_age_secs: float = 21_600.0,
) -> dict[str, Any]:
    """Refresh Hindsight-compatible candidates and report pool coverage."""
    from tusker_gateway.pools import PoolManager

    manager = PoolManager(load_config())
    results = await run_structured_qualification(
        pool_name=pool_name,
        base_url=base_url,
        max_concurrency=1,
        timeout_secs=timeout_secs,
        max_age_secs=max_age_secs,
        limit=limit,
        ignore_cooldowns=False,
        manager=manager,
    )
    qualified = qualified_count(
        manager=manager,
        pool_name=pool_name,
        max_age_secs=max_age_secs,
    )
    summary = {
        "pool": pool_name,
        "tested": len(results),
        "passed": sum(1 for result in results if result.get("status") == "passed"),
        "failed": sum(1 for result in results if result.get("status") != "passed"),
        "qualified": qualified,
        "minimum_required": 2,
        "degraded": qualified < 2,
    }
    if summary["degraded"]:
        logger.warning(
            "structured qualification degraded pool=%s qualified=%d minimum_required=%d",
            pool_name,
            qualified,
            summary["minimum_required"],
        )
    return summary


async def run_modality_maintenance_cycle(
    *,
    pool_names: tuple[str, ...],
    base_url: str = "http://127.0.0.1:8642",
    input_modality: str = _DEFAULT_MODALITY,
    limit: int = 8,
    timeout_secs: float = 45.0,
    max_age_secs: float = 86_400.0,
    transient_max_age_secs: float | None = None,
    per_probe_delay_secs: float = 0.0,
) -> dict[str, Any]:
    """Probe input modality on a small batch of candidates across the pool set.

    The runner refreshes the catalog once, picks candidates whose catalog row
    advertises the modality (or every general-chat candidate when
    ``include_unadvertised`` is forced), stamps ``passed``/``unsupported``
    records into ``ModelCapabilityDB`` and reports a single summary line. The
    capability DB is then consulted by ``_input_modalities_allowed`` in
    ``pools.py``, so vision+tool requests stop being filtered by stale catalog
    rows within one probe cycle.

    Polite-provider semantics:

    * ``max_concurrency`` is forced to ``1`` so probes are sequential — no
      fan-out against a single upstream.
    * ``per_probe_delay_secs`` adds a small gap between consecutive probes
      so a flaky provider gets a chance to recover before the next attempt.
    * Authoritative ``passed`` / ``unsupported`` records are cached for
      ``max_age_secs`` (typically 24h — a capability claim is stable).
      ``unavailable`` records (transient 5xx / timeout / auth) are cached for
      ``transient_max_age_secs`` (typically a few hours) so the runner
      retries them sooner without re-probing every cycle.
    * Quarantined routes are skipped — the gateway's persistent and
      in-memory cooldowns prevent the maintenance cycle from turning a known
      outage into a probe storm.
    """
    from tusker_gateway.modality_qualification import run_qualification

    config = load_config()
    api_key = os.environ.get("API_KEYS", "").split(",", 1)[0].strip()
    if not api_key:
        # Mirrors the structured/tool runners — the gateway caller key is
        # always present in production via the env vault, so we treat absence
        # as a configuration error rather than silently skipping the probe.
        raise RuntimeError("API_KEYS must contain the gateway caller key")
    results = await run_qualification(
        pool_names=list(pool_names),
        base_url=base_url,
        input_modality=input_modality,
        max_concurrency=1,
        timeout_secs=timeout_secs,
        max_age_secs=max_age_secs,
        transient_max_age_secs=transient_max_age_secs,
        per_probe_delay_secs=per_probe_delay_secs,
        limit=limit,
        ignore_cooldowns=False,
    )
    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status", "unavailable"))
        counts[status] = counts.get(status, 0) + 1
    return {
        "modality": input_modality,
        "pools": ",".join(pool_names),
        "tested": len(results),
        "passed": counts.get("passed", 0),
        "unsupported": counts.get("unsupported", 0),
        "unavailable": counts.get("unavailable", 0),
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
    base_url = (
        os.environ.get(
            "TUSKER_TOOL_QUALIFICATION_BASE_URL",
            "http://127.0.0.1:8642",
        ).strip()
        or "http://127.0.0.1:8642"
    )
    structured_enabled = _structured_qualification_enabled(config)
    structured_pool = _structured_pool_name(config)
    structured_limit = _env_int(
        "TUSKER_STRUCTURED_QUALIFICATION_LIMIT",
        4,
        minimum=1,
    )
    structured_timeout_secs = _env_float(
        "TUSKER_STRUCTURED_QUALIFICATION_TIMEOUT_SECS",
        45.0,
        minimum=5.0,
    )
    structured_max_age_secs = _env_float(
        "TUSKER_STRUCTURED_QUALIFICATION_MAX_AGE_SECS",
        21_600.0,
        minimum=60.0,
    )
    modality_enabled = _modality_qualification_enabled(config)
    modality_pools = _modality_pool_order(config)
    modality_name = _modality_name()
    modality_limit = _env_int(
        "TUSKER_MODALITY_QUALIFICATION_LIMIT",
        8,
        minimum=1,
    )
    modality_timeout_secs = _env_float(
        "TUSKER_MODALITY_QUALIFICATION_TIMEOUT_SECS",
        45.0,
        minimum=5.0,
    )
    modality_max_age_secs = _env_float(
        "TUSKER_MODALITY_QUALIFICATION_MAX_AGE_SECS",
        86_400.0,
        minimum=60.0,
    )
    modality_transient_max_age_secs = _env_float(
        "TUSKER_MODALITY_QUALIFICATION_TRANSIENT_MAX_AGE_SECS",
        21_600.0,
        minimum=60.0,
    )
    modality_per_probe_delay_secs = _env_float(
        "TUSKER_MODALITY_QUALIFICATION_PER_PROBE_DELAY_SECS",
        2.0,
        minimum=0.0,
    )
    modality_initial_delay_secs = _env_float(
        "TUSKER_MODALITY_QUALIFICATION_INITIAL_DELAY_SECS",
        600.0,
        minimum=0.0,
    )
    modality_interval_secs = _env_float(
        "TUSKER_MODALITY_QUALIFICATION_INTERVAL_SECS",
        43_200.0,
        minimum=60.0,
    )

    logger.info(
        "qualification maintenance started interval=%.0fs initial_delay=%.0fs limit=%d pools=%s",
        interval_secs,
        initial_delay_secs,
        limit,
        ",".join(pools),
    )
    if structured_enabled:
        logger.info(
            "structured qualification enabled pool=%s limit=%d timeout=%.0fs max_age=%.0fs",
            structured_pool,
            structured_limit,
            structured_timeout_secs,
            structured_max_age_secs,
        )
    if modality_enabled:
        logger.info(
            "modality qualification enabled modality=%s pools=%s limit=%d "
            "timeout=%.0fs max_age=%.0fs transient_max_age=%.0fs "
            "per_probe_delay=%.1fs interval=%.0fs initial_delay=%.0fs",
            modality_name,
            ",".join(modality_pools),
            modality_limit,
            modality_timeout_secs,
            modality_max_age_secs,
            modality_transient_max_age_secs,
            modality_per_probe_delay_secs,
            modality_interval_secs,
            modality_initial_delay_secs,
        )
    first_cycle = True
    pool_index = 0
    modality_next_due_at = (
        time.monotonic() + modality_initial_delay_secs if modality_enabled else None
    )
    while not stop_event.is_set():
        # The tool/structured probes run on a coarse cadence (typically
        # every few hours) while the modality probe runs on its own
        # ``initial_delay`` + ``interval`` clock. Wake up for whichever is
        # due next so the modality probe can fire independently of the tool
        # cycle — otherwise it would wait for the next tool cycle and miss
        # the configured initial_delay by hours.
        tool_delay = initial_delay_secs if first_cycle else interval_secs
        if modality_enabled and modality_next_due_at is not None:
            modality_delay = max(
                0.0,
                modality_next_due_at - time.monotonic(),
            )
        else:
            modality_delay = float("inf")
        delay = min(tool_delay, modality_delay)
        first_cycle = False
        if await _wait_or_stop(stop_event, delay):
            return
        now_mono = time.monotonic()
        # The tool cycle is due only when we just woke from the tool cadence
        # (``delay == tool_delay`` and that delay has elapsed). The modality
        # cycle is due only when ``now_mono >= modality_next_due_at``.
        # These two are independent so a modality probe firing early does
        # not pull the tool cycle along with it.
        tool_due = tool_delay <= modality_delay
        if tool_due:
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
                if structured_enabled:
                    structured_summary = await run_structured_maintenance_cycle(
                        pool_name=structured_pool,
                        base_url=base_url,
                        limit=structured_limit,
                        timeout_secs=structured_timeout_secs,
                        max_age_secs=structured_max_age_secs,
                    )
                    logger.info(
                        "structured qualification result=%s",
                        structured_summary,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Keep the scheduler alive and avoid logging provider response data.
                logger.warning(
                    "qualification maintenance cycle failed pool=%s error_class=%s",
                    pool_name,
                    type(exc).__name__,
                )
        # Modality probe: independent cadence. If due, run after the tool
        # cycle completes (so a slow tool probe doesn't delay the modality
        # probe's clock). Reschedule relative to *now*.
        if (
            modality_enabled
            and modality_next_due_at is not None
            and now_mono >= modality_next_due_at
        ):
            try:
                modality_summary = await run_modality_maintenance_cycle(
                    pool_names=modality_pools,
                    base_url=base_url,
                    input_modality=modality_name,
                    limit=modality_limit,
                    timeout_secs=modality_timeout_secs,
                    max_age_secs=modality_max_age_secs,
                    transient_max_age_secs=modality_transient_max_age_secs,
                    per_probe_delay_secs=modality_per_probe_delay_secs,
                )
                logger.info(
                    "modality qualification result=%s",
                    modality_summary,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "modality qualification cycle failed error_class=%s",
                    type(exc).__name__,
                )
            finally:
                modality_next_due_at = time.monotonic() + modality_interval_secs
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
            if structured_enabled:
                structured_summary = await run_structured_maintenance_cycle(
                    pool_name=structured_pool,
                    base_url=base_url,
                    limit=structured_limit,
                    timeout_secs=structured_timeout_secs,
                    max_age_secs=structured_max_age_secs,
                )
                logger.info(
                    "structured qualification result=%s",
                    structured_summary,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep the scheduler alive and avoid logging provider response data.
            logger.warning(
                "qualification maintenance cycle failed pool=%s error_class=%s",
                pool_name,
                type(exc).__name__,
            )
        # Modality probe: independent cadence. If due, run after the tool
        # cycle completes (so a slow tool probe doesn't delay the modality
        # probe's clock). Reschedule relative to *now*.
        if (
            modality_enabled
            and modality_next_due_at is not None
            and now_mono >= modality_next_due_at
        ):
            try:
                modality_summary = await run_modality_maintenance_cycle(
                    pool_names=modality_pools,
                    base_url=base_url,
                    input_modality=modality_name,
                    limit=modality_limit,
                    timeout_secs=modality_timeout_secs,
                    max_age_secs=modality_max_age_secs,
                    transient_max_age_secs=modality_transient_max_age_secs,
                    per_probe_delay_secs=modality_per_probe_delay_secs,
                )
                logger.info(
                    "modality qualification result=%s",
                    modality_summary,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "modality qualification cycle failed error_class=%s",
                    type(exc).__name__,
                )
            finally:
                modality_next_due_at = time.monotonic() + modality_interval_secs
    while not stop_event.is_set():
        # The tool/structured probes run on a coarse cadence (typically
        # every few hours) while the modality probe runs on its own
        # ``initial_delay`` + ``interval`` clock. Wake up for whichever is
        # due next so the modality probe can fire independently of the tool
        # cycle — otherwise it would wait for the next tool cycle and miss
        # the configured initial_delay by hours.
        tool_delay = initial_delay_secs if first_cycle else interval_secs
        if modality_enabled and modality_next_due_at is not None:
            modality_delay = max(
                0.0,
                modality_next_due_at - time.monotonic(),
            )
        else:
            modality_delay = float("inf")
        delay = min(tool_delay, modality_delay)
        first_cycle = False
        if await _wait_or_stop(stop_event, delay):
            return
        now_mono = time.monotonic()
        if (
            modality_enabled
            and modality_next_due_at is not None
            and now_mono >= modality_next_due_at
        ):
            try:
                modality_summary = await run_modality_maintenance_cycle(
                    pool_names=modality_pools,
                    base_url=base_url,
                    input_modality=modality_name,
                    limit=modality_limit,
                    timeout_secs=modality_timeout_secs,
                    max_age_secs=modality_max_age_secs,
                    transient_max_age_secs=modality_transient_max_age_secs,
                    per_probe_delay_secs=modality_per_probe_delay_secs,
                )
                logger.info(
                    "modality qualification result=%s",
                    modality_summary,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "modality qualification cycle failed error_class=%s",
                    type(exc).__name__,
                )
            finally:
                # Reschedule the next probe relative to *now*, not to the
                # wall-clock time the cycle started — a slow probe must not
                # stack up extra modality probes immediately.
                modality_next_due_at = time.monotonic() + modality_interval_secs
        if now_mono - (time.monotonic() - tool_delay) >= now_mono - tool_delay:
            # Wake the tool cycle only when the tool_delay has elapsed.
            pass
        # Tool cycle runs alongside the modality probe, not nested inside it.
        # Check whether the tool cycle is due (first cycle uses initial_delay;
        # subsequent cycles use interval_secs).
        tool_due = (
            first_cycle is False  # noqa: already cleared above
            and now_mono - (now_mono - tool_delay) >= 0  # always
        )
        # Simpler: schedule the tool cycle independently. We track when the
        # tool cycle last ran via ``last_tool_at``; if ``interval_secs`` has
        # elapsed, run a tool cycle this iteration.
        # (Implementation moved below.)
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
            if structured_enabled:
                structured_summary = await run_structured_maintenance_cycle(
                    pool_name=structured_pool,
                    base_url=base_url,
                    limit=structured_limit,
                    timeout_secs=structured_timeout_secs,
                    max_age_secs=structured_max_age_secs,
                )
                logger.info(
                    "structured qualification result=%s",
                    structured_summary,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep the scheduler alive and avoid logging provider response data.
            logger.warning(
                "qualification maintenance cycle failed pool=%s error_class=%s",
                pool_name,
                type(exc).__name__,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep the scheduler alive and avoid logging provider response data.
            logger.warning(
                "qualification maintenance cycle failed pool=%s error_class=%s",
                pool_name,
                type(exc).__name__,
            )
