"""Request-scoped broker pointer helpers for CLI MCP bridges."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _point_broker(pointer: Path, manifest_file: Path, call_file: Path) -> None:
    """Atomically name the request the shared MCP bridge must serve."""
    temporary = pointer.with_name(f".{pointer.name}.{os.urandom(4).hex()}")
    temporary.write_text(
        json.dumps({"manifest": str(manifest_file), "call": str(call_file)}),
        encoding="utf-8",
    )
    os.replace(temporary, pointer)


def _clear_broker(pointer: Path) -> None:
    """Best-effort release of the shared bridge so no stale tools are served."""
    temporary = pointer.with_name(f".{pointer.name}.{os.urandom(4).hex()}")
    try:
        temporary.write_text("{}", encoding="utf-8")
        os.replace(temporary, pointer)
    except OSError:
        pass
