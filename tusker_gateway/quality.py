"""Quality DB: persistent per-model success scoring.

Tracks call counts and quality scores for each (provider, model) pair.
The score combines success rate and latency with exponential decay.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tusker_gateway.storage import chunked_pairs, shared_database

logger = logging.getLogger(__name__)

QUALITY_WINDOW = 20
QUALITY_RECENT_FAILURE_WINDOW_SECS = 300.0
QUALITY_RECENT_FAILURE_PENALTY = 30.0



@dataclass
class ModelQuality:
    provider: str
    model: str
    score: float
    total_calls: int
    success_calls: int
    failure_calls: int
    last_success_at: float | None


def _coarse_token_count(text: str) -> int:
    """Estimate token count (rough: 1 token ~= 4 chars)."""
    return max(1, len(text) // 4)


class QualityDB:
    def __init__(self, path: str):
        self._path = path
        self._db = shared_database(path)
        self._ensure_db()

    def _ensure_db(self) -> None:
        if not self._db.is_postgres:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        event_id_definition = (
            "BIGSERIAL PRIMARY KEY"
            if self._db.is_postgres
            else "INTEGER PRIMARY KEY AUTOINCREMENT"
        )
        with self._db.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_quality (
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    quality_score REAL DEFAULT 100.0,
                    total_calls INTEGER DEFAULT 0,
                    success_calls INTEGER DEFAULT 0,
                    failure_calls INTEGER DEFAULT 0,
                    last_success_at REAL,
                    PRIMARY KEY (provider, model)
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS model_events (
                    id {event_id_definition},
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_events_provider_model_id "
                "ON model_events(provider, model, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_events_recent_failures "
                "ON model_events(provider, model, success, created_at)"
            )
            conn.commit()

    def prime_model(self, provider: str, model: str) -> None:
        """Pre-seed a quality score for a model that has no recorded events.

        Static pool entries are primed to 100.0 so they outrank unknown
        auto-discovered catalog candidates (which start near 40).  The
        pre-seed only applies when there is no event history: once real
        outcomes exist, the learned score survives every pool rebuild and
        config hot-reload.  Previously the unconditional upsert reset the
        score to 100.0 on each rebuild, erasing failure signals for broken
        operator-curated models and sending them traffic again.
        """
        with self._db.connection() as conn:
            existing_events = conn.execute(
                "SELECT COUNT(*) FROM model_events WHERE provider = ? AND model = ?",
                (provider, model),
            ).fetchone()
            if existing_events is None:
                # Advisory state store unavailable: reads come back as noop
                # cursors and writes are no-ops, so there is nothing to seed.
                # Indexing the noop row raised TypeError and crash-looped the
                # gateway at startup until PostgreSQL returned.
                return
            if existing_events[0]:
                return
            if self._db.is_postgres:
                conn.execute(
                    "INSERT INTO model_quality (provider, model, quality_score, "
                    "total_calls, success_calls) VALUES (?, ?, 100.0, 0, 0) "
                    "ON CONFLICT(provider, model) DO NOTHING",
                    (provider, model),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO model_quality "
                    "(provider, model, quality_score, total_calls, success_calls) "
                    "VALUES (?, ?, 100.0, 0, 0)",
                    (provider, model),
                )
            conn.commit()

    def unprime_model(self, provider: str, model: str) -> None:
        """Remove the pre-seed quality score for a model.

        Called when a static pool entry is removed so the model reverts to the
        adaptive floor and behaves as an unknown candidate.
        """
        with self._db.connection() as conn:
            conn.execute(
                "DELETE FROM model_quality WHERE provider = ? AND model = ?",
                (provider, model),
            )
            conn.commit()

    def record(self, provider: str, model: str, success: bool, latency_ms: float) -> None:
        """Record a call outcome and update quality score."""
        now = time.time()
        logger.debug('record %s/%s success=%s latency=%.1fms', provider, model, success, latency_ms)
        with self._db.connection() as conn:
            conn.execute(
                (
                    "INSERT INTO model_quality (provider, model) VALUES (?, ?) "
                    "ON CONFLICT(provider, model) DO NOTHING"
                    if self._db.is_postgres
                    else "INSERT OR IGNORE INTO model_quality (provider, model) VALUES (?, ?)"
                ),
                (provider, model),
            )
            conn.execute(
                """
                INSERT INTO model_events (provider, model, success, latency_ms, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (provider, model, int(success), latency_ms, now),
            )
            # Update totals
            if success:
                conn.execute(
                    """
                    UPDATE model_quality
                    SET total_calls = total_calls + 1,
                        success_calls = success_calls + 1,
                        last_success_at = ?
                    WHERE provider = ? AND model = ?
                    """,
                    (now, provider, model),
                )
            else:
                conn.execute(
                    """
                    UPDATE model_quality
                    SET total_calls = total_calls + 1,
                        failure_calls = failure_calls + 1
                    WHERE provider = ? AND model = ?
                    """,
                    (provider, model),
                )
            # Recompute quality score
            self._recompute_score(conn, provider, model)
            conn.commit()

    def _recompute_score(self, conn: Any, provider: str, model: str) -> None:
        """Recompute quality score from recent success and latency evidence.

        Latency remains bounded so a slow success cannot dominate a route, but
        it continues to distinguish seconds from minutes instead of flattening
        at the exponential tail. Local capacity rejections are recorded as
        failures by the passthrough client, so repeated saturation also
        demotes a route without removing it from the final fallback set.
        """
        window = conn.execute(
            """
            SELECT COUNT(*), AVG(success), AVG(latency_ms) FROM (
                SELECT success, latency_ms FROM model_events
                WHERE provider = ? AND model = ?
                ORDER BY id DESC LIMIT ?
            )
            """,
            (provider, model, QUALITY_WINDOW),
        ).fetchone()
        total_window, success_rate, avg_latency = (
            window[0] if window else 0,
            (window[1] if window and window[1] is not None else 0.0),
            (window[2] if window and window[2] is not None else 1000.0),
        )
        if not total_window:
            return
        latency_bonus = 1.0 / (1.0 + max(0.0, avg_latency) / 1500.0)
        score = success_rate * 80.0 + latency_bonus * 20.0
        conn.execute(
            """
            UPDATE model_quality SET quality_score = ? WHERE provider = ? AND model = ?
            """,
            (score, provider, model),
        )
        logger.debug('recomputed score for %s/%s: %.2f', provider, model, score)

    def get_quality(self, provider: str, model: str) -> float | None:
        """Return quality score for (provider, model), or None if no data."""
        with self._db.connection() as conn:
            row = conn.execute(
                """
                SELECT quality_score FROM model_quality WHERE provider = ? AND model = ?
                """,
                (provider, model),
            ).fetchone()
            return row[0] if row else None

    def evidence(self, provider: str, model: str) -> tuple[int, float] | None:
        """Return (total_calls, last_success_at) for a pair, or None.

        Used by the quality warm-up cycle to decide whether a candidate
        has learned evidence at all (``None``) or stale/thin evidence
        (``last_success_at`` older than the warm-up max age).
        """
        with self._db.connection() as conn:
            row = conn.execute(
                """
                SELECT total_calls, last_success_at
                FROM model_quality WHERE provider = ? AND model = ?
                """,
                (provider, model),
            ).fetchone()
        if not row:
            return None
        return int(row[0] or 0), float(row[1]) if row[1] is not None else 0.0

    def rank(
        self,
        candidates: list[tuple[str, str]],
        *,
        default_score: float | None = None,
    ) -> list[tuple[str, str, float]]:
        """Sort candidates by quality with expiring recent-failure demotion.

        Read quality rows and recent failures in bulk. Selection can inspect
        hundreds of routes, so per-candidate database connections and queries
        turn the ranking step into an avoidable O(n) round-trip cost.
        """
        if not candidates:
            return []
        pairs = list(dict.fromkeys(candidates))
        cutoff = time.time() - QUALITY_RECENT_FAILURE_WINDOW_SECS
        scores: dict[tuple[str, str], float] = {}
        recent_failures: dict[tuple[str, str], int] = {}
        with self._db.connection() as conn:
            # A CTE of candidate pairs joined against the tables lets the
            # planner use the composite primary key. An OR-chain of
            # per-candidate predicates defeats the index on PostgreSQL and
            # degrades to a sequential scan as the pool grows.
            #
            # The pair list is chunked because each pair binds two
            # parameters and the backend caps the number of parameters in
            # one statement.
            for chunk in chunked_pairs(pairs):
                placeholders = ", ".join(["(?, ?)"] * len(chunk))
                pair_params = [value for pair in chunk for value in pair]
                quality_rows = conn.execute(
                    "WITH pairs(provider, model) AS (VALUES " + placeholders + ") "
                    "SELECT q.provider, q.model, q.quality_score "
                    "FROM model_quality q "
                    "JOIN pairs p ON q.provider = p.provider AND q.model = p.model",
                    pair_params,
                ).fetchall()
                failure_rows = conn.execute(
                    "WITH pairs(provider, model) AS (VALUES " + placeholders + ") "
                    "SELECT e.provider, e.model, COUNT(*) "
                    "FROM model_events e "
                    "JOIN pairs p ON e.provider = p.provider AND e.model = p.model "
                    "WHERE e.success = 0 AND e.created_at >= ? "
                    "GROUP BY e.provider, e.model",
                    [*pair_params, cutoff],
                ).fetchall()
                scores.update(
                    {
                        (str(row[0]), str(row[1])): float(row[2])
                        for row in quality_rows
                        if row is not None and row[2] is not None
                    }
                )
                recent_failures.update(
                    {
                        (str(row[0]), str(row[1])): int(row[2] or 0)
                        for row in failure_rows
                        if row is not None
                    }
                )

        known_scores = [scores[pair] for pair in candidates if pair in scores]
        if known_scores:
            known_scores.sort()
            n = len(known_scores)
            floor = (
                known_scores[n // 2]
                if n % 2
                else (known_scores[n // 2 - 1] + known_scores[n // 2]) / 2.0
            )
            floor = min(max(10.0, floor - 20.0), 50.0)
        else:
            floor = 50.0
        if default_score is not None:
            floor = default_score

        result: list[tuple[str, str, float]] = []
        for provider, model in candidates:
            score = scores.get((provider, model))
            # A missing row and a negative stored score are both "no
            # evidence": fall back to the computed floor.
            if score is None or score < 0:
                score = floor
            score -= min(
                QUALITY_RECENT_FAILURE_PENALTY,
                recent_failures.get((provider, model), 0) * QUALITY_RECENT_FAILURE_PENALTY,
            )
            result.append((provider, model, score))
        result.sort(key=lambda x: x[2], reverse=True)
        logger.debug("ranked %d candidates", len(result))
        return result

    def status(self) -> dict[str, Any]:
        """Return summary status for /status endpoint."""
        with self._db.connection() as conn:
            count_row = conn.execute("SELECT COUNT(*) FROM model_quality").fetchone()
            healthy_row = conn.execute(
                "SELECT COUNT(*) FROM model_quality WHERE quality_score >= 50.0"
            ).fetchone()
        if count_row is None or healthy_row is None:
            # Degraded state store: report empty rather than failing /status.
            return {"total_models": 0, "healthy_models": 0}
        return {
            "total_models": int(count_row[0]),
            "healthy_models": int(healthy_row[0]),
        }
