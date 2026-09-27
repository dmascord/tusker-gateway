"""One-shot LLM Stats rankings importer.

Refreshes the capability verdict table for the configured pool inventory
without restarting the gateway. The same code runs as a daily background
task during gateway operation (see ``app.py``).

Quota discipline: the user-facing key allows ~250 requests/day and the
upstream applies an aggressive burst limit (HTTP 429 after ~35 rapid
calls). The sync therefore paces requests via
``TUSKER_LLM_STATS_REQUEST_PACING_SECS`` and stops gracefully on 429
without overwriting the stored verdicts.

Usage:
    python -m tusker_gateway.tools.sync_llm_stats
    python -m tusker_gateway.tools.sync_llm_stats --json
    python -m tusker_gateway.tools.sync_llm_stats --limit 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from tusker_gateway.config import load_config
from tusker_gateway.model_rankings import sync_llm_stats_rankings, pool_model_pairs


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the sync summary as JSON on stdout (logging goes to stderr).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of pool pairs to refresh (default: all configured).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    config = load_config()
    pairs = pool_model_pairs(config)
    if args.limit > 0:
        pairs = pairs[: args.limit]
    if not config.get("llm_stats_api_key"):
        print(
            "LLMSTATS_API_KEY not configured; set it in the environment "
            "or the deployment vault.",
            file=sys.stderr,
        )
        return 2
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        summary = await sync_llm_stats_rankings(config, pairs, session)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(
            "llm-stats sync complete: "
            f"models={summary['models']} "
            f"passed={summary['passed']} "
            f"excluded={summary['excluded']} "
            f"unknown={summary['unknown']} "
            f"requests={summary['requests']} "
            f"rate_limited={summary['rate_limited']}"
        )
    return 1 if summary.get("rate_limited") else 0


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
