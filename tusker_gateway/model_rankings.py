"""External capability rankings from LLM Stats for pool candidate filtering.

``quality.py`` measures transport health (success rate + latency). This
module imports an *external* benchmark ranking (https://llm-stats.com) so
pool selection can prefer better-ranked models and drop known-weak ones
that are reliable but low capability.

Data source (LLM Stats Stats API, Bearer auth):

* ``GET /stats/v1/rankings?category=<cat>&limit=50`` — aggregated TrueSkill
  rank per category, capped at the top 50 by the upstream API.

Verdicts come from those windows alone: a pool model present in a tracked
category's window is ``pass`` when its window rank is at or above
``TUSKER_LLM_STATS_MAX_RANK`` (default 25) and ``excluded`` when worse.
The window rank is also the selection bonus input, so verdicts and
ordering always share one evidence source.

The per-model detail endpoint is deliberately unused: its ``rank`` fields
are per-benchmark leaderboard positions, not cross-model category
positions, so mixing them with the window cutoff would be misleading.

Models absent from every tracked window (private, local, brand-new, or
outside the top 50) are recorded as ``unknown`` and are never excluded —
the filter fails open so curated routes are not lost to a catalog gap.

Quota discipline: five window requests per sync (one per tracked
category). The upstream applies an aggressive burst limit (observed 429
after ~35 rapid calls), so the sync paces requests and on HTTP 429 it
stops immediately and keeps the previous verdict rows intact.

Website seed provenance: the seed-import tool
(``tusker_gateway.tools.import_llm_stats_seed``) may add deep category
ranks (up to ~375) sourced from the llm-stats.com homepage. Seed rows are
tagged ``site_seed``, are only written for models absent from every API
window, never overwrite window evidence, and age out after
``TUSKER_LLM_STATS_SEED_MAX_AGE_SECS`` (default 7 days) via the daily
sync's overlay, degrading back to ``unknown``. See
``docs/gateway-model-routing.md`` (Website seed provenance).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from tusker_gateway.storage import shared_database

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.llm-stats.com/stats/v1"
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "code",
    "general",
    "reasoning",
    "tool_calling",
    "agents",
)
DEFAULT_MAX_RANK = 25
# The upstream rankings endpoint hard-caps ``limit`` at 50 (422 above).
RANKINGS_WINDOW_LIMIT = 50
DEFAULT_REFRESH_SECS = 86_400.0
DEFAULT_PACING_SECS = 3.0
# Maximum score bonus for a rank-1 model. Bounded so a well-ranked model
# cannot outrank a healthy model whose learned quality is far higher:
# rank orders candidates within a quality band, it does not override it.
DEFAULT_RANK_BOOST_CAP = 25.0
# Decay per rank; rank-1 gets the full cap and it falls linearly to zero
# at rank ``cap / per_rank + 1``. With these defaults every model that
# survives the rank cutoff (rank <= 25) receives a distinct 1..25 bonus.
# Set to 0 to disable the rank preference entirely.
DEFAULT_RANK_BOOST_PER_RANK = 1.0
# Provenance label written by the website-seed import tool. A row tagged
# with this evidence carries a deep category rank sourced from the
# llm-stats.com homepage leaderboards; the daily API sync only carries
# it forward while it remains fresher than ``seed_max_age_secs`` and
# only for models absent from every tracked API window.
SEED_EVIDENCE = "site_seed"
# Default lifetime (seconds) for site_seed rows before the daily sync
# degrades them back to "unknown". 7 days keeps deep coverage honest
# against weekly category drift while still capping stale seeds.
DEFAULT_SEED_MAX_AGE_SECS = 7 * 86_400.0
# Default homepage URL scraped by the seed import tool. Operators may
# override via ``TUSKER_LLM_STATS_SITE_URL``; the value is intentionally
# a documentation page rather than an undocumented internal endpoint.
DEFAULT_SITE_URL = "https://llm-stats.com/"

_USER_AGENT = "tusker-gateway-llm-stats/0.1"



def normalize_slug(model: str) -> str:
    """Map a gateway model name to an LLM Stats model id.

    Validated against the live catalog: ``gpt-oss:20b`` → ``gpt-oss-20b``,
    ``MiniMax-M3`` → ``minimax-m3``, ``hf:openai/gpt-oss-120b`` →
    ``gpt-oss-120b``, ``hf:moonshotai/Kimi-K3`` → ``kimi-k3``,
    ``hf:Qwen/Qwen3.8-27B`` → ``qwen3.8-27b``. Slugs with no LLM Stats
    counterpart (``syn:large:text`` → ``syn-large-text``) are absent from
    every tracked window and become ``unknown`` verdicts.
    """
    name = str(model or "").strip().lower()
    if not name:
        return ""
    if name.startswith("hf:"):
        name = name[3:]
    if "::" in name:
        name = name.rsplit("::", 1)[-1]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.replace(":", "-").replace("_", "-").replace(" ", "-")

def _find_array_end(text: str, start: int) -> int | None:
    """Index of the ``]`` closing the array that opens at ``start``.

    ``start`` points just past the opening bracket. String-aware: a
    ``]`` inside a quoted value does not close the array. Returns None
    when the array is unterminated.
    """
    depth = 1
    in_str = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return idx
    return None


def extract_leaderboard_arrays(
    html: str,
) -> list[tuple[str, list[tuple[str, int]]]]:
    """Pull ordered ``(model_id, rank)`` arrays from the site's leaderboard pages.

    The llm-stats.com homepage ships its server-side data as a collection of
    ranked models inside ``self.__next_f.push([1,"..."])`` script strings.
    Each leader-board is keyed by its own ``category_id`` and lives under
    ``"models":[ ... ]``, so we slice each array with a bracket counter
    instead of guessing boundaries.

    Returns ``(key, [(slug, rank)])`` pairs in document order. The *key* is
    the site's own ``category_id``; callers should still require an
    exact-prefix match against the API window to guard against site renames.
    """
    text = html.replace('\\"', '"')
    results: list[tuple[str, list[tuple[str, int]]]] = []
    # Locate every category entry: "<key>":{"category_id":"<key>","models":[
    pattern = re.compile(r'"category_id":"(?P<key>[^"]+)"\s*,\s*"models"\s*:\s*\[')
    for match in pattern.finditer(text):
        key = match.group("key")
        start = match.end()  # right after '['
        end = _find_array_end(text, start)
        if end is None:
            continue
        slice_ = text[start:end]
        entries: list[tuple[str, int]] = []
        for em in re.finditer(
            r'\{"model_id":"(?P<id>[^"]+)"[^{}]*?"rank":(?P<rank>-?\d+)',
            slice_,
        ):
            raw_id = em.group("id")
            try:
                slug = json.loads(f'"{raw_id}"')
            except json.JSONDecodeError:
                slug = raw_id
            try:
                rank = int(em.group("rank"))
            except ValueError:
                continue
            if rank <= 0:
                continue
            entries.append((slug, rank))
        if entries:
            results.append((key, entries))
    return results

# Minimum positional overlap required to accept a category array whose
# site key matches our tracked name. Set to 0.9 to tolerate minor ranking
# drift between the API snapshot and the site fetch while still rejecting
# genuinely different leaderboards that share few top-models.
MATCH_MIN_OVERLAP = 0.9


def match_arrays_to_windows(
    windows: dict[str, dict[str, int]],
    arrays: list[tuple[str, list[tuple[str, int]]]],
) -> dict[str, list[tuple[str, int]]]:
    """Identify category leaderboards by key identity and prefix match.

    ``windows`` maps category name to ``{slug: rank}`` from the authoritative
    API.  ``arrays`` comes from :func:`extract_leaderboard_arrays`.

    For each tracked category, pick the best matching array among:

    1. An array whose site ``key == category`` AND whose top-N ids overlap
       the window at or above ``MATCH_MIN_OVERLAP`` — this tolerates minor
       ranking drift.
    2. Any array with **exact** ordered prefix equality over the first N
       positions — this guards against renamed site keys (strongest invariant).
    """
    matches: dict[str, list[tuple[str, int]]] = {}
    for category, window in windows.items():
        if not window:
            continue
        ordered_window = [
            slug for slug, _rank in sorted(window.items(), key=lambda kv: kv[1])
        ]
        depth = len(ordered_window)
        matched: list[tuple[str, list[tuple[str, int]]]] | None = None
        # Rule 1: name identity + high overlap (drift-tolerant)
        same_key = [a for a in arrays if a[0] == category]
        for _, entries in same_key:
            if len(entries) < depth:
                continue
            positional = sum(
                a == b
                for a, b in zip(
                    (s for s, _ in entries[:depth]), ordered_window
                )
            )
            if positional >= MATCH_MIN_OVERLAP * depth:
                matched = (category, entries)
                break
        # Rule 2: any-array exact prefix (rename-safe fallback)
        if matched is None:
            for _, entries in arrays:
                if len(entries) < depth:
                    continue
                if [s for s, _ in entries[:depth]] == ordered_window:
                    matched = (category, entries)
                    break
        if matched is not None:
            matches[category] = matched[1]
    return matches


class ModelRankingsDB:
    """Persistent LLM Stats verdicts, one row per normalized model slug."""

    def __init__(self, path: str):
        self._path = path
        self._db = shared_database(path)
        self._ensure_db()

    def _ensure_db(self) -> None:
        with self._db.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_stats_verdicts (
                    slug TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    category_rank INTEGER,
                    category_name TEXT,
                    evidence TEXT,
                    synced_at REAL NOT NULL
                )
                """
            )

    def rows(self) -> dict[str, dict[str, Any]]:
        with self._db.connection() as conn:
            cursor = conn.execute(
                """
                SELECT slug, status,
                       category_rank, category_name, evidence, synced_at
                FROM llm_stats_verdicts
                """
            )
            result: dict[str, dict[str, Any]] = {}
            for row in cursor.fetchall():
                result[row[0]] = {
                    "slug": row[0],
                    "status": row[1],
                    "category_rank": row[2],
                    "category_name": row[3],
                    "evidence": row[4],
                    "synced_at": row[5],
                }
            return result

    def verdict_for(self, model: str) -> str | None:
        """Return the stored verdict for one gateway model name."""
        slug = normalize_slug(model)
        if not slug:
            return None
        with self._db.connection() as conn:
            cursor = conn.execute(
                "SELECT status FROM llm_stats_verdicts WHERE slug = ?",
                (slug,),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def excludes(self, model: str) -> bool:
        """Whether pool selection should drop this model.

        Only explicit ``excluded`` verdicts filter; ``unknown`` (no LLM
        Stats data) and ``pass`` stay selectable.
        """
        return self.verdict_for(model) == "excluded"

    def effective_rank(self, model: str) -> int | None:
        """Cross-model LLM Stats rank for one gateway model name, or None.

        Uses ``category_rank`` only: the rankings-window position against
        every other model in a tracked category. Models with no window
        presence are ordered by quality alone.
        """
        slug = normalize_slug(model)
        if not slug:
            return None
        with self._db.connection() as conn:
            cursor = conn.execute(
                "SELECT category_rank FROM llm_stats_verdicts WHERE slug = ?",
                (slug,),
            )
            row = cursor.fetchone()
        if not row or not isinstance(row[0], int) or row[0] <= 0:
            return None
        return row[0]

    def replace_all(self, rows: list[dict[str, Any]], synced_at: float) -> None:
        """Atomically swap the verdict table for a fresh sync snapshot."""
        payload = []
        for row in rows:
            payload.append(
                (
                    row["slug"],
                    row["status"],
                    row.get("category_rank"),
                    row.get("category_name"),
                    row.get("evidence"),
                    row.get("synced_at", synced_at),
                )
            )
        with self._db.connection() as conn:
            conn.execute("DELETE FROM llm_stats_verdicts")
            for entry in payload:
                conn.execute(
                    """
                    INSERT INTO llm_stats_verdicts (
                        slug, status, category_rank, category_name,
                        evidence, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    entry,
                )

    def apply_seed(
        self, rows: list[dict[str, Any]], synced_at: float
    ) -> dict[str, int]:
        """Import website-seed verdicts without clobbering API evidence.

        Rows carrying API window evidence (``category_window``) are
        authoritative and left untouched. Seed rows upsert everywhere
        else: over ``unknown`` rows, over stale seed rows, and into
        empty slots. Returns ``{"written": N, "skipped_window": M}``.
        """
        written = 0
        skipped = 0
        with self._db.connection() as conn:
            for row in rows:
                cursor = conn.execute(
                    "SELECT evidence FROM llm_stats_verdicts WHERE slug = ?",
                    (row["slug"],),
                )
                existing = cursor.fetchone()
                if existing and existing[0] == "category_window":
                    skipped += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO llm_stats_verdicts (
                        slug, status, category_rank, category_name,
                        evidence, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        status = excluded.status,
                        category_rank = excluded.category_rank,
                        category_name = excluded.category_name,
                        evidence = excluded.evidence,
                        synced_at = excluded.synced_at
                    """,
                    (
                        row["slug"],
                        row["status"],
                        row.get("category_rank"),
                        row.get("category_name"),
                        SEED_EVIDENCE,
                        row.get("synced_at", synced_at),
                    ),
                )
                written += 1
        return {"written": written, "skipped_window": skipped}

    def status(self) -> dict[str, Any]:
        """Summary for /status-style reporting."""
        rows = self.rows()
        counts: dict[str, int] = {}
        for row in rows.values():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {
            "models": len(rows),
            "statuses": counts,
            "last_synced_at": max(
                (row["synced_at"] for row in rows.values()), default=None
            ),
        }


def pool_model_pairs(config: dict[str, Any]) -> list[tuple[str, str]]:
    """Collect (provider, model) pairs from the configured static pools."""
    pairs: list[tuple[str, str]] = []
    for name, pool in (config.get("pools") or {}).items():
        for entry in getattr(pool, "models", None) or []:
            provider = entry.get("provider") if isinstance(entry, dict) else None
            model = entry.get("model") if isinstance(entry, dict) else None
            if provider and model:
                pairs.append((str(provider), str(model)))
    return pairs


async def _get_json(
    session: Any, url: str, api_key: str
) -> tuple[int, dict[str, Any] | None]:
    """GET one LLM Stats endpoint. Returns (status, body-or-None)."""
    timeout = aiohttp_client_timeout()
    async with session.get(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
        timeout=timeout,
    ) as resp:
        status = int(resp.status)
        if status in (404, 429):
            return status, None
        if status in (401, 403):
            raise RuntimeError(
                f"llm-stats auth failed (HTTP {status}); check LLMSTATS_API_KEY"
            )
        resp.raise_for_status()
        return status, await resp.json(content_type=None)

async def fetch_category_windows(
    config: dict[str, Any],
    session: Any,
    categories: frozenset[str],
) -> tuple[dict[str, dict[str, int]], int, bool]:
    """Fetch one API window per tracked category, paced and rate-limited aware.

    Returns ``(windows, requests, rate_limited)`` where ``windows`` maps
    each category name to ``{slug: rank}`` — the authoritative top-50
    window for that category.  On HTTP 429 the caller stops immediately;
    the ``rate_limited`` flag signals that earlier windows may be stale.
    """
    windows: dict[str, dict[str, int]] = {}
    requests = 0
    rate_limited = False
    base_url = str(
        config.get("llm_stats_base_url") or DEFAULT_BASE_URL
    ).rstrip("/")
    pacing = float(config.get("llm_stats_pacing_secs") or DEFAULT_PACING_SECS)
    api_key = str(config.get("llm_stats_api_key") or "").strip()
    for category in sorted(categories):
        status, body = await _get_json(
            session,
            f"{base_url}/rankings?category={category}&limit={RANKINGS_WINDOW_LIMIT}",
            api_key,
        )
        requests += 1
        if status == 429:
            rate_limited = True
            break
        for entry in (body or {}).get("models") or []:
            model_id = str(entry.get("model_id") or "").strip().lower()
            rank = entry.get("rank")
            if model_id and isinstance(rank, int) and rank > 0:
                windows.setdefault(category, {})[model_id] = rank
        if pacing > 0:
            await asyncio.sleep(pacing)
    return windows, requests, rate_limited


def aiohttp_client_timeout() -> Any:
    import aiohttp

    return aiohttp.ClientTimeout(total=30)


async def sync_llm_stats_rankings(
    config: dict[str, Any],
    pairs: list[tuple[str, str]],
    session: Any,
) -> dict[str, Any]:
    """Refresh verdicts for the pool inventory. Returns a sync summary.

    Verdicts come from the aggregated category rankings windows only: the
    detail endpoint exposes per-benchmark ranks rather than cross-model
    category positions, so its numbers are not comparable with the cutoff
    and would make verdicts inconsistent with the selection bonus. On
    HTTP 429 the sync aborts without touching stored verdicts so the
    gateway keeps operating on the previous snapshot.
    """
    api_key = str(config.get("llm_stats_api_key") or "").strip()
    if not api_key:
        raise ValueError("llm-stats sync requires LLMSTATS_API_KEY")
    categories = frozenset(
        str(cat).strip().lower()
        for cat in (config.get("llm_stats_categories") or DEFAULT_CATEGORIES)
        if str(cat).strip()
    )
    max_rank = int(config.get("llm_stats_max_rank") or DEFAULT_MAX_RANK)
    pacing = float(config.get("llm_stats_pacing_secs") or DEFAULT_PACING_SECS)
    if not pairs:
        return {
            "models": 0,
            "passed": 0,
            "excluded": 0,
            "unknown": 0,
            "seed_kept": 0,
            "requests": 0,
            "rate_limited": False,
            "synced_at": time.time(),
        }

    db = ModelRankingsDB(config["llm_stats_db_path"])
    now = time.time()
    # Snapshot prior rows so the sync can propagate fresh website-seed
    # rows for models absent from every tracked API window. Seed rows
    # beyond ``seed_max_age_secs`` degrade back to ``unknown`` on the
    # next sync, which keeps deep coverage honest against category drift.
    # ``rows()`` is already keyed by slug.
    previous_by_slug: dict[str, dict[str, Any]] = db.rows()
    seed_max_age = float(config.get("llm_stats_seed_max_age_secs", DEFAULT_SEED_MAX_AGE_SECS))

    # 1. Aggregated category windows (one cheap request per category).
    windows, requests, rate_limited = await fetch_category_windows(
        config, session, categories
    )
    # Best rank across tracked categories wins: the cutoff and the
    # selection bonus both describe the model's strongest tracked
    # position, so a model that ranks well in any tracked window is
    # not excluded by an unrelated category.
    category_rank: dict[str, tuple[int, str]] = {}
    for category, entries in windows.items():
        for slug, rank in entries.items():
            best = category_rank.get(slug)
            if best is None or rank < best[0]:
                category_rank[slug] = (rank, category)

    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {"passed": 0, "excluded": 0, "unknown": 0, "seed_kept": 0}

    # 2. Per-pool verdicts from window evidence only. The detail endpoint
    # exposes per-benchmark ranks that are not comparable with the cutoff
    # or with the selection bonus, so verdicts come from the cross-model
    # category rankings; absence from any tracked window is honest
    # "unknown" evidence rather than inferred from benchmark noise.
    seen: set[str] = set()
    for _provider, model in pairs:
        slug = normalize_slug(model)
        if not slug or slug in seen:
            continue
        seen.add(slug)

        window = category_rank.get(slug)
        if window is not None:
            status = "pass" if window[0] <= max_rank else "excluded"
            rows.append(
                {
                    "slug": slug,
                    "status": status,
                    "category_rank": window[0],
                    "category_name": window[1],
                    "evidence": "category_window",
                    "synced_at": now,
                }
            )
            counts["passed" if status == "pass" else "excluded"] += 1
            continue

        # Absent from every tracked window. A fresh website-seed row is
        # carried forward unchanged so deep coverage survives until the
        # seed ages out; otherwise the verdict is honest ``unknown``.
        seeded = previous_by_slug.get(slug)
        if (
            seeded is not None
            and seeded.get("evidence") == SEED_EVIDENCE
            and seed_max_age > 0
            and (now - float(seeded.get("synced_at") or 0.0)) <= seed_max_age
        ):
            rows.append(
                {
                    "slug": slug,
                    "status": seeded.get("status") or "unknown",
                    "category_rank": seeded.get("category_rank"),
                    "category_name": seeded.get("category_name"),
                    "evidence": SEED_EVIDENCE,
                    "synced_at": float(seeded.get("synced_at") or 0.0),
                }
            )
            counts["seed_kept"] += 1
            continue

        rows.append(
            {
                "slug": slug,
                "status": "unknown",
                "category_rank": None,
                "category_name": None,
                "evidence": "not_in_category_window",
                "synced_at": now,
            }
        )
        counts["unknown"] += 1

    if rate_limited:
        logger.warning(
            "llm-stats sync hit HTTP 429 after %d requests; keeping previous verdicts",
            requests,
        )
        return {
            "models": len(seen),
            "passed": counts["passed"],
            "excluded": counts["excluded"],
            "unknown": counts["unknown"],
            "seed_kept": counts["seed_kept"],
            "requests": requests,
            "rate_limited": True,
            "synced_at": now,
        }

    db.replace_all(rows, now)
    summary = {
        "models": len(seen),
        "passed": counts["passed"],
        "excluded": counts["excluded"],
        "unknown": counts["unknown"],
        "seed_kept": counts["seed_kept"],
        "requests": requests,
        "rate_limited": False,
        "synced_at": now,
    }
    excluded = [row["slug"] for row in rows if row["status"] == "excluded"]
    if excluded:
        logger.info(
            "llm-stats capability cutoff (max_rank=%d, categories=%s) excluded: %s",
            max_rank,
            ",".join(sorted(categories)),
            ",".join(excluded),
        )
    return summary


async def llm_stats_refresh_loop(
    config: dict[str, Any], session: Any, stop_event: asyncio.Event
) -> None:
    """Refresh LLM Stats verdicts on a cadence without blocking startup."""
    interval = float(config.get("llm_stats_refresh_secs") or DEFAULT_REFRESH_SECS)
    while True:
        try:
            pairs = pool_model_pairs(config)
            summary = await sync_llm_stats_rankings(config, pairs, session)
            logger.info("llm-stats rankings synced: %s", summary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm-stats rankings sync failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            continue


def load_llm_stats_env_config(config: dict[str, Any]) -> None:
    """Populate ``llm_stats_*`` config keys from the environment.

    Called from ``config.load_config``; kept here so the env names live
    next to the code that consumes them.
    """
    from pathlib import Path as _Path

    quality_path = str(config.get("quality_db_path") or "")
    default_db = (
        str(_Path(quality_path).with_name("llm_stats.db"))
        if quality_path and quality_path != ":memory:"
        else ":memory:"
    )
    def _env_number(name: str, default: float, minimum: float) -> float:
        try:
            value = float(os.environ.get(name, "") or default)
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    config["llm_stats_db_path"] = os.environ.get(
        "TUSKER_LLM_STATS_DB_PATH", default_db
    )
    # Rank-boost tuning for pool selection. A model whose best LLM Stats
    # rank is ``r`` receives ``max(0, cap - per_rank * (r - 1))`` added to
    # its quality score, so better-ranked models reach higher selection
    # tiers. Set per_rank to 0 to disable the preference.
    config["llm_stats_rank_boost_cap"] = _env_number(
        "TUSKER_LLM_STATS_RANK_BOOST_CAP", DEFAULT_RANK_BOOST_CAP, 0.0
    )
    config["llm_stats_rank_boost_per_rank"] = _env_number(
        "TUSKER_LLM_STATS_RANK_BOOST_PER_RANK", DEFAULT_RANK_BOOST_PER_RANK, 0.0
    )
    config["llm_stats_api_key"] = os.environ.get("LLMSTATS_API_KEY", "").strip()
    config["llm_stats_base_url"] = (
        os.environ.get("TUSKER_LLM_STATS_BASE_URL", "").strip().rstrip("/")
        or DEFAULT_BASE_URL
    )
    raw_categories = os.environ.get("TUSKER_LLM_STATS_CATEGORIES", "").strip()
    if raw_categories:
        categories = tuple(
            dict.fromkeys(
                cat.strip().lower()
                for cat in raw_categories.split(",")
                if cat.strip()
            )
        )
    else:
        categories = DEFAULT_CATEGORIES
    config["llm_stats_categories"] = categories

    config["llm_stats_max_rank"] = int(
        _env_number("TUSKER_LLM_STATS_MAX_RANK", DEFAULT_MAX_RANK, 1)
    )
    config["llm_stats_refresh_secs"] = _env_number(
        "TUSKER_LLM_STATS_REFRESH_SECS", DEFAULT_REFRESH_SECS, 600.0
    )
    config["llm_stats_pacing_secs"] = _env_number(
        "TUSKER_LLM_STATS_REQUEST_PACING_SECS", DEFAULT_PACING_SECS, 0.0
    )
    # Site-seed freshness policy: how long a website-seed row is kept by the
    # daily sync before degrading back to unknown. Set to 0 to disable seed
    # retention entirely (seed rows are still written by the import tool).
    config["llm_stats_seed_max_age_secs"] = _env_number(
        "TUSKER_LLM_STATS_SEED_MAX_AGE_SECS",
        DEFAULT_SEED_MAX_AGE_SECS,
        0.0,
    )
    # Homepage whose leaderboards are scraped by the seed-import tool.
    config["llm_stats_site_url"] = (
        os.environ.get("TUSKER_LLM_STATS_SITE_URL", "").strip() or DEFAULT_SITE_URL
    )
