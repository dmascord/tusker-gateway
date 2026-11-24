"""Scheduled retention for bounded gateway state.

Two stores document a retention window but only prune opportunistically:

- ``TurnCaptureStore`` prunes inside ``record()``, so expired rows survive a
  traffic lull (and a 7-day window is not a guarantee when traffic stops).
- ``AuditLogger.prune()`` has no caller at all, so the documented 90-day
  audit window never applied.

This module gives both a single, idempotent entry point that the maintenance
loop calls on a daily cadence. It is deliberately best-effort: a prune failure
is logged and reported, never propagated into the maintenance scheduler.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_retention_prune(*, audit: Any | None = None) -> dict[str, int]:
    """Prune expired turn captures and audit events.

    ``audit`` is the app's ``AuditLogger``; when omitted a new one is built
    from the environment, which is correct in the maintenance task where the
    app object is not available. Returns per-store removal counts for logging
    and tests. Never raises: each store is independent.
    """
    removed: dict[str, int] = {}

    # Turn captures can be large (780 KB average request body in production).
    # capture_store() returns None when TUSKER_STREAM_CAPTURE is off, in which
    # case there is nothing to prune.
    try:
        from tusker_gateway.turn_capture import capture_store

        store = capture_store()
        if store is not None:
            removed["turn_captures"] = int(store.prune() or 0)
        else:
            removed["turn_captures"] = 0
    except Exception:  # noqa: BLE001 - best effort, keep the scheduler alive
        logger.warning("turn capture prune failed", exc_info=True)
        removed["turn_captures"] = 0

    try:
        if audit is None:
            from tusker_gateway.audit import AuditLogger, load_audit_config_from_env

            audit = AuditLogger(load_audit_config_from_env())
        removed["audit_events"] = int(audit.prune() or 0)
    except Exception:  # noqa: BLE001 - an audit prune failure must not stop captures
        logger.warning("audit event prune failed", exc_info=True)
        removed["audit_events"] = 0

    logger.info(
        "retention prune removed=%s",
        ",".join(f"{name}={count}" for name, count in removed.items()),
    )
    return removed


__all__ = ["run_retention_prune"]
