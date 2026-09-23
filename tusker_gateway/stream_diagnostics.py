"""Private, bounded captures for diagnosing provider reasoning cycles.

Captured text can contain prompt-derived or otherwise sensitive material. It
is therefore kept out of ordinary logs and written only to a mode-0700
directory with mode-0600 files. Captures are bounded by count and total bytes.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_MAX_RECORDS = 200
_MAX_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_TEXT_CHARS = 4096


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
            files = list(root.glob("cycle-*.jsonl"))
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
