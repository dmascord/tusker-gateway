"""Captures for diagnosing provider stream anomalies.

Two kinds of capture live here. Rejected provider turns are stored as rows in
the shared state database (``tusker_gateway.turn_capture``) so they outlive log
rotation and can be replayed. Repeated-reasoning cycles still go to a mode-0700
directory with mode-0600 files, bounded by count and total bytes; clear that
directory to resume cycle captures once its budget is exhausted.

Both carry prompt-derived material, so neither is written to ordinary logs. The
in-memory copy of a provider-bound request is dropped as soon as its turn
resolves, so a successful turn never pins prompt bytes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from tusker_gateway.turn_capture import (
    load_turn_capture_config_from_env,
    store_turn_capture,
)

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MAX_RECORDS = 200
_MAX_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_TEXT_CHARS = 4096


def _capture_files(root: Path) -> list[Path]:
    """Return the reasoning-cycle capture files sharing one budget.

    Only files this module writes are counted: an unrelated ``.json`` in the
    capture directory must not consume the budget for real captures. Rejected
    turns no longer land here, so legacy ``unusable-*.json`` files are ignored.
    """
    return list(root.glob("cycle-*.jsonl"))


def store_reasoning_cycle(
    text: str,
    *,
    request_id: str | None,
    provider: str,
    model: str,
    source: str,
    cycle_chars: int,
    repeats: int,
) -> str | None:
    """Persist one confirmed detector match; return a safe basename or None."""
    root = Path(os.environ.get(
        "TUSKER_STREAM_DIAGNOSTICS_DIR",
        "/home/tusker/.hermes/stream-diagnostics",
    ))
    safe_text = text[-_MAX_TEXT_CHARS:]
    record: dict[str, Any] = {
        "timestamp": time.time(),
        "request_id": request_id,
        "provider": provider,
        "model": model,
        "source": source,
        "cycle_chars": cycle_chars,
        "repeats": repeats,
        "text": safe_text,
    }
    payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    name = f"cycle-{int(time.time())}-{uuid.uuid4().hex}.jsonl"
    try:
        with _LOCK:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            files = _capture_files(root)
            total = sum(path.stat().st_size for path in files if path.is_file())
            if len(files) >= _MAX_RECORDS or total + len(payload) > _MAX_TOTAL_BYTES:
                return None
            path = root / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        return name
    except OSError:
        # Diagnostics must never turn an upstream issue into a gateway outage.
        return None


def extract_cycle_text(text: str, cycle_chars: int, repeats: int) -> str:
    """Return the matched trailing repetition, clipped to the capture bound."""
    length = max(0, cycle_chars * repeats)
    return text[-min(length, _MAX_TEXT_CHARS):]


# -- Rejected-turn captures ------------------------------------------------
#
# A rejected tool turn is usually hidden from the client by an immediate
# failover, so the only durable evidence of whether the verdict was correct is
# what the provider actually sent and what the gateway actually asked for.
# The bytes go to the capture database together with the verdict counters, so
# the record outlives container log rotation. Enable with TUSKER_STREAM_CAPTURE.

# The response buffer is held in memory for the life of one stream, so it keeps
# its own small bound. The request hold is bounded by the capture policy.
_RESPONSE_CAPTURE_MAX_BYTES = 64 * 1024
_MAX_PENDING_REQUESTS = 64
_PENDING_TTL_SECS = 900.0
_PENDING: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def stream_capture_enabled() -> bool:
    """Return whether rejected-turn byte capture is enabled."""
    return os.environ.get("TUSKER_STREAM_CAPTURE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _held_bytes() -> int:
    """Return the byte count currently pinned by pending request holds.

    Called with ``_LOCK`` held.
    """
    return sum(
        len(value["payload"])
        for value in _PENDING.values()
        if value.get("payload") is not None
    )


def remember_upstream_request(
    *,
    request_id: str | None,
    provider: str,
    model: str,
    pool: str | None,
    stream: bool,
    source: str,
    payload: bytes,
) -> None:
    """Hold the provider-bound request until the turn ends.

    Only streaming turns are held: those are the ones the gateway can reject
    after the response is committed. The payload is kept whole so a rejected
    turn can be replayed, bounded by the capture policy; a request larger than
    that bound is held without bytes, so its rejection still records the size
    and verdict. An entry is dropped when the rejection reads it, and otherwise
    expires, so a successful turn cannot pin prompt bytes in memory.

    A pre-stream 4xx leaves no such hold: ``_check_response`` passes the exact
    outbound bytes it already has to the capture store, so a successful
    non-streaming turn never pins prompt bytes.
    """
    if not stream or not request_id or not stream_capture_enabled():
        return
    config = load_turn_capture_config_from_env()
    held = payload if len(payload) <= config.max_request_bytes else None
    with _LOCK:
        now = time.time()
        for key, value in list(_PENDING.items()):
            if now - value["captured_at"] > _PENDING_TTL_SECS:
                del _PENDING[key]
        _PENDING[request_id] = {
            "captured_at": now,
            "provider": provider,
            "model": model,
            "pool": pool,
            "source": source,
            "payload": held,
            "payload_bytes": len(payload),
        }
        while len(_PENDING) > _MAX_PENDING_REQUESTS:
            _PENDING.popitem(last=False)
        while _held_bytes() > config.pending_bytes and len(_PENDING) > 1:
            _PENDING.popitem(last=False)


def take_upstream_request(request_id: str | None) -> dict[str, Any] | None:
    """Remove and return the pending request capture for ``request_id``."""
    if not request_id:
        return None
    with _LOCK:
        return _PENDING.pop(request_id, None)


def append_response_capture(capture: bytearray | None, chunk: bytes) -> None:
    """Extend ``capture`` with ``chunk`` without exceeding the capture bound."""
    if capture is None:
        return
    room = _RESPONSE_CAPTURE_MAX_BYTES - len(capture)
    if room > 0:
        capture.extend(chunk[:room])


def store_unusable_stream_capture(
    *,
    request_id: str | None,
    provider: str,
    model: str,
    reason: str,
    response: bytes,
    verdict: dict[str, Any],
    pool: str | None = None,
    request_payload: bytes | None = None,
    request_source: str | None = None,
) -> str | None:
    """Persist one rejected provider turn; return its capture id or ``None``.

    The row outlives the container log, so the rejection stays auditable and
    replayable. Prompt and response bytes are withheld when the pool is
    excluded (the ZDR boundary) or cannot be established at all; the verdict
    counters are kept in every case. See ``tusker_gateway.turn_capture``.

    ``pool``/``request_payload`` let a caller that already holds the outbound
    bytes (a pre-stream 4xx in ``_check_response``) attribute the pool without
    going through the streaming hold. When omitted, the hold is consumed as
    before.
    """
    if not stream_capture_enabled():
        return None
    if pool is None and request_payload is None:
        pending = take_upstream_request(request_id)
        if pending is None:
            # Process-backed adapters and the Anthropic entry point assemble
            # their own provider payload, and an expired or evicted hold leaves
            # nothing behind, so the pool that served this turn is not known.
            pool = None
            request_source = "unavailable"
            request_payload = None
        else:
            pool = pending["pool"]
            request_source = pending["source"]
            request_payload = pending["payload"]
    else:
        # A streaming turn may also carry a hold; use its bytes when the caller
        # did not supply the outbound payload itself, then drop the hold so a
        # later reader cannot attach stale bytes to a different rejection.
        pending = take_upstream_request(request_id)
        if request_payload is None and pending is not None:
            request_payload = pending["payload"]
        if request_source is None:
            request_source = "chat_completions"
    return store_turn_capture(
        request_id=request_id,
        provider=provider,
        model=model,
        pool=pool,
        request_source=request_source,
        reason=reason,
        verdict=verdict,
        request_payload=request_payload,
        response_payload=response,
    )
