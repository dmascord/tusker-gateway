"""Private, bounded captures for diagnosing provider stream anomalies.

Two kinds of capture live here: repeated-reasoning cycles, and provider turns
that the gateway rejected as unusable. Captured text can contain
prompt-derived or otherwise sensitive material. It is therefore kept out of
ordinary logs and written only to a mode-0700 directory with mode-0600 files.
Captures are bounded by count and total bytes; clear the directory to resume
capture once that budget is exhausted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MAX_RECORDS = 200
_MAX_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_TEXT_CHARS = 4096


def _capture_files(root: Path) -> list[Path]:
    """Return the capture files sharing one count/byte budget.

    Only files this module writes are counted: an unrelated ``.json`` in the
    capture directory must not consume the budget for real captures.
    """
    return [
        *root.glob("cycle-*.jsonl"),
        *root.glob("unusable-*.json"),
    ]


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
# Both are captured here, bounded, together with the verdict counters, as one
# private JSON record. Enable with TUSKER_STREAM_CAPTURE.

_REQUEST_CAPTURE_MAX_BYTES = 16 * 1024
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


def _bounded_capture(payload: bytes, limit: int) -> dict[str, Any]:
    """Describe a payload as byte count, digest, and a head+tail excerpt."""
    if len(payload) <= limit:
        text = payload.decode("utf-8", errors="replace")
        elided = 0
    else:
        head = limit // 2
        tail = limit - head
        elided = len(payload) - limit
        text = (
            payload[:head].decode("utf-8", errors="replace")
            + f"\n<<TUSKER-CAPTURE-TRUNCATED: {elided} bytes elided>>\n"
            + payload[-tail:].decode("utf-8", errors="replace")
        )
    return {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "truncated_bytes": elided,
        "text": text,
    }


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
    """Hold a bounded copy of the provider-bound request until the turn ends.

    Only streaming turns are held: those are the ones the gateway can reject
    after the response is committed. An entry is dropped when the rejection
    reads it, and otherwise expires, so a successful turn cannot pin prompt
    bytes in memory.
    """
    if not stream or not request_id or not stream_capture_enabled():
        return
    capture = _bounded_capture(payload, _REQUEST_CAPTURE_MAX_BYTES)
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
            "request": capture,
        }
        while len(_PENDING) > _MAX_PENDING_REQUESTS:
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
) -> str | None:
    """Persist one rejected provider turn; return a safe basename or None."""
    if not stream_capture_enabled():
        return None
    record: dict[str, Any] = {
        "timestamp": time.time(),
        "request_id": request_id,
        "provider": provider,
        "model": model,
        "reason": reason,
        "verdict": verdict,
        "upstream_response": _bounded_capture(response, _RESPONSE_CAPTURE_MAX_BYTES),
    }
    pending = take_upstream_request(request_id)
    if pending is None:
        # Process-backed adapters and the Anthropic entry point assemble their
        # own provider payload, so only the response side is available there.
        record["pool"] = None
        record["request_source"] = "unavailable"
        record["upstream_request"] = None
    else:
        record["pool"] = pending["pool"]
        record["request_source"] = pending["source"]
        record["upstream_request"] = pending["request"]
    payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    root = Path(os.environ.get(
        "TUSKER_STREAM_DIAGNOSTICS_DIR",
        "/home/tusker/.hermes/stream-diagnostics",
    ))
    name = f"unusable-{int(time.time())}-{uuid.uuid4().hex}.json"
    try:
        with _LOCK:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            files = _capture_files(root)
            total = sum(path.stat().st_size for path in files if path.is_file())
            if len(files) >= _MAX_RECORDS or total + len(payload) > _MAX_TOTAL_BYTES:
                # The store is shared with the reasoning-cycle captures; the
                # operator has to clear it before capture resumes.
                logger.warning(
                    "stream capture store full; dropped request_id=%s files=%d bytes=%d",
                    request_id or "unknown",
                    len(files),
                    total,
                )
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
