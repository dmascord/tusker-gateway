"""Website-seed importer for deep LLM Stats coverage.

The public rankings API caps every category window at 50 models, so pool
models ranked 51..~375 are ``unknown`` even though llm-stats.com itself
publishes the deeper leaderboard on its homepage. This tool imports that
deeper data as *seed* rows:

* Windows are fetched from the authoritative API first (5 paced
  requests).
* The homepage is fetched once (or read from ``--html``) and its
  embedded leaderboard arrays are extracted.
* Arrays are identified per category by exact ordered prefix match
  against the API window - if the site changes shape the match fails
  and that category is skipped, never misread.
* Only pool models absent from every API window are seeded; rows with
  API window evidence (``category_window``) are never overwritten.
* Seed rows carry ``evidence=site_seed`` and age out after
  ``TUSKER_LLM_STATS_SEED_MAX_AGE_SECS`` (default 7 days) via the daily
  sync, degrading back to ``unknown`` unless re-seeded.

Usage:
    python -m tusker_gateway.tools.import_llm_stats_seed --html page.html
    python -m tusker_gateway.tools.import_llm_stats_seed --dry-run
    python -m tusker_gateway.tools.import_llm_stats_seed --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from tusker_gateway.config import load_config
from tusker_gateway.model_rankings import (
    DEFAULT_CATEGORIES,
    ModelRankingsDB,
    extract_leaderboard_arrays,
    fetch_category_windows,
    match_arrays_to_windows,
    normalize_slug,
    pool_model_pairs,
)

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--html",
        type=str,
        default="",
        help="Read the llm-stats.com homepage HTML from this file instead "
        "of fetching it over HTTP (offline/deterministic seeding).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report the seed without writing the database.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the import summary as JSON on stdout.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser.parse_args()


def _tracked_categories(config: dict[str, Any]) -> frozenset[str]:
    """Categories whose windows anchor the site-array match."""
    return frozenset(
        str(cat).strip().lower()
        for cat in (config.get("llm_stats_categories") or DEFAULT_CATEGORIES)
        if str(cat).strip()
    )


async def _fetch_site_html(config: dict[str, Any], session: Any) -> str:
    import aiohttp

    url = str(config.get("llm_stats_site_url") or "https://llm-stats.com/")
    async with session.get(url, headers={"User-Agent": "tusker-gateway-seed/0.1"}) as resp:
        resp.raise_for_status()
        return await resp.text()


def build_seed_rows(
    windows: dict[str, dict[str, int]],
    arrays: list[list[tuple[str, int]]],
    pairs: list[tuple[str, str]],
    max_rank: int,
    synced_at: float,
) -> tuple[list[dict[str, Any]], dict[str, list[tuple[str, int]]]]:
    """Compute seed rows for pool models the API windows cannot see.

    Returns ``(rows, matches)`` where ``rows`` is the seed payload and
    ``matches`` is the category -> matched-array mapping (exposed for
    logging / tests).
    """
    matches = match_arrays_to_windows(windows, arrays)
    in_window: set[str] = set()
    for window in windows.values():
        in_window.update(window)

    best: dict[str, tuple[int, str]] = {}
    for category, array in matches.items():
        for slug, rank in array:
            if slug in in_window:
                continue
            current = best.get(slug)
            if current is None or rank < current[0]:
                best[slug] = (rank, category)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _provider, model in pairs:
        slug = normalize_slug(model)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        hit = best.get(slug)
        if hit is None:
            continue
        rank, category = hit
        rows.append(
            {
                "slug": slug,
                "status": "pass" if rank <= max_rank else "excluded",
                "category_rank": rank,
                "category_name": category,
                "evidence": "site_seed",
                "synced_at": synced_at,
            }
        )
    return rows, matches


async def _run(args: argparse.Namespace) -> int:
    config = load_config()
    pairs = pool_model_pairs(config)
    if not pairs:
        print("No pool models configured; nothing to seed.", file=sys.stderr)
        return 2
    if not config.get("llm_stats_api_key"):
        print(
            "LLMSTATS_API_KEY not configured; window anchoring requires it.",
            file=sys.stderr,
        )
        return 2

    import aiohttp

    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        windows, requests, rate_limited = await fetch_category_windows(
            config, session, _tracked_categories(config)
        )
        if rate_limited:
            print(
                "LLM Stats API rate-limited during window fetch; aborting "
                "without writing seeds.",
                file=sys.stderr,
            )
            return 1
        if args.html:
            with open(args.html, "r", encoding="utf-8", errors="replace") as fh:
                html = fh.read()
        else:
            html = await _fetch_site_html(config, session)

    arrays = extract_leaderboard_arrays(html)
    if not arrays:
        print(
            "No leaderboard arrays found in the site payload; nothing seeded.",
            file=sys.stderr,
        )
        return 1
    if not windows:
        print(
            "No API windows fetched; cannot anchor site arrays. Nothing seeded.",
            file=sys.stderr,
        )
        return 1

    import time as _time

    synced_at = _time.time()
    rows, matches = build_seed_rows(
        windows,
        arrays,
        pairs,
        int(config.get("llm_stats_max_rank") or 25),
        synced_at,
    )
    summary: dict[str, Any] = {
        "site_arrays": len(arrays),
        "matched_categories": sorted(matches.keys()),
        "match_depths": {
            cat: len(arr) for cat, arr in sorted(matches.items())
        },
        "window_requests": requests,
        "seed_rows": len(rows),
        "seeded_slugs": sorted(row["slug"] for row in rows),
    }
    if args.dry_run:
        summary["dry_run"] = True
    else:
        db = ModelRankingsDB(config["llm_stats_db_path"])
        result = db.apply_seed(rows, synced_at)
        summary["written"] = result["written"]
        summary["skipped_window"] = result["skipped_window"]

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(
            "llm-stats seed import: "
            f"site_arrays={summary['site_arrays']} "
            f"matched={','.join(summary['matched_categories']) or 'none'} "
            f"seed_rows={summary['seed_rows']} "
            + (
                "dry_run=true"
                if args.dry_run
                else f"written={summary.get('written', 0)} "
                f"skipped_window={summary.get('skipped_window', 0)}"
            )
        )
    return 0


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
