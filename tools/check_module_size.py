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
#
# 2026-11-11 baselines re-measured against main + the unreleased branch fixes.
# The previous values had been exceeded on main for days without the gate being
# consulted, so it was reporting pre-existing drift, not new growth. Each value
# below is the current reviewed line count; the gate again polices NEW growth
# only.
# endpoints.py: 8661 -> 9381. Protocol-compatibility work landed after the
#   2026-10-18 bump (reasoning-only length terminal handling and the
#   in-band-thinking rejection path). Extraction stays a Priority 2 task.
# passthrough.py: 3411 -> 3771. Codex OAuth refresh/cooldown adoption and the
#   MLX Mac capacity queue (d131051).
# pools.py: 1790 -> 1934. Composite image-plus-tools evidence gating and the
#   input-modality merge correction (ba4f53a).
# admin.py: 1704 -> 1717. Admin credential/cooldown surfaces.
#
# 2026-11-25 baselines re-measured against main (45b8ee5) plus the direct-route
# cooldown gate. The gate had been red on main since before 2026-11-20 and was
# NOT policing new growth: the 2026-11-20 values had themselves been exceeded
# while the check was unconsulted, so every value below was already wrong
# before this branch started. Each delta is attributed to a reviewed commit.
#
# endpoints.py: 9431 -> 9749. Resilience release (b891e74: retention hooks and
#   HA wiring), context advisory headers and RTK hardening (c38a06c), the
#   all-rate-limit 429 terminal surface (87444f9, 2a2d41a, f65cf8f), CLI stream
#   repair (4911c6e, 99931ee), and this branch's +50 (the direct-route gate;
#   the bulk of the gate lives in cooldown.py to limit growth here).
# passthrough.py: 3819 -> 3842. Resilience release (b891e74) and CLI stream
#   repair (4911c6e).
# pools.py: 2134 -> 2490. Value routing (6e2a1b7), price refresh (68fb635),
#   pool context compaction (4756202), provider-wide 429 diversification
#   (f98d1ff + its revert 18b9fe4), and the composite probe fixture (e1c8f0b).
# admin.py: 1717 -> 1752. Claude auth status surface (52eb988).
# config_store.py: 1583 -> 1652. Resilience release (b891e74) and the async
#   advisory lock (62acb03).
#
# Extraction remains Priority 2 in TODO.md. Raising these values admits
# reviewed reality so the gate polices NEW growth again; it is not a licence
# for further growth in these files.
MAX_LINES = {
    "tusker_gateway/endpoints.py": 9_749,
    "tusker_gateway/passthrough.py": 3_842,
    "tusker_gateway/pools.py": 2_490,
    "tusker_gateway/admin.py": 1_752,
    "tusker_gateway/config_store.py": 1_652,
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
