#!/usr/bin/env python3
"""Prevent known Python architecture hotspots from growing unnoticed."""
from __future__ import annotations

from pathlib import Path
import sys

# Baselines deliberately match the reviewed main branch. Reduce these values as
# responsibilities are extracted; do not raise them without architectural review.
# endpoints.py: bumped 8312 -> 8661 on 2026-10-18. Growth since the 8400
# baseline is the reviewed protocol-compatibility work (merge 85ecca6):
# protocol resolution at the request boundary, pre-visible failover bounds,
# and the vLLM/OpenRouter error-frame surface. The extraction steps remain
# Priority 2 in TODO.md; this admits the reviewed reality so the gate is
# green and can police NEW growth.
# passthrough.py: 3405 -> 3411 (protocol runtime hooks, merge 85ecca6).
# admin.py: 1666 -> 1704 (admin protocol probe + evidence endpoints, PR #8).
MAX_LINES = {
    "tusker_gateway/endpoints.py": 8_661,
    "tusker_gateway/passthrough.py": 3_411,
    "tusker_gateway/pools.py": 1_790,
    "tusker_gateway/admin.py": 1_704,
    "tusker_gateway/config_store.py": 1_583,
}


def count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def main() -> int:
    failures: list[str] = []
    for filename, maximum in MAX_LINES.items():
        actual = count_lines(Path(filename))
        status = "OK" if actual <= maximum else "GROWTH"
        print(f"{status:6} {actual:5} / {maximum:5}  {filename}")
        if actual > maximum:
            failures.append(f"{filename}: {actual} lines exceeds baseline {maximum}")
    if failures:
        print("\nArchitecture hotspot growth detected:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        print("Extract responsibility or document an approved baseline change.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
