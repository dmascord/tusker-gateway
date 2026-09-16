"""Process-local behavioral reputation for upstream models."""
from __future__ import annotations

import os
import threading
from collections import defaultdict

_lock = threading.Lock()
_suspicious_counts: dict[tuple[str, str], int] = defaultdict(int)
_runtime_blacklist: set[tuple[str, str]] = set()


def _key(provider: str, model: str) -> tuple[str, str]:
    return (
        str(provider or "").strip().lower().replace("_", "-"),
        str(model or "").strip().lower(),
    )


def suspicious_model_threshold() -> int:
    try:
        return max(1, int(os.environ.get("TUSKER_SUSPICIOUS_MODEL_THRESHOLD", "3")))
    except (TypeError, ValueError):
        return 3


def record_suspicious_behavior(provider: str, model: str) -> tuple[int, bool]:
    """Record one blocked action; return ``(count, newly_blacklisted)``."""
    key = _key(provider, model)
    with _lock:
        _suspicious_counts[key] += 1
        count = _suspicious_counts[key]
        newly_blacklisted = count >= suspicious_model_threshold() and key not in _runtime_blacklist
        if newly_blacklisted:
            _runtime_blacklist.add(key)
        return count, newly_blacklisted


def is_runtime_blacklisted(provider: str, model: str) -> bool:
    return _key(provider, model) in _runtime_blacklist


def reset_reputation() -> None:
    """Reset process-local state for tests and controlled maintenance."""
    with _lock:
        _suspicious_counts.clear()
        _runtime_blacklist.clear()
