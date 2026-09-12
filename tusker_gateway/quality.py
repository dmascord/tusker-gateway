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

from tusker_gateway.storage import shared_database

logger = logging.getLogger(__name__)

QUALITY_WINDOW = 20


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
            conn.commit()

    def prime_model(self, provider: str, model: str) -> None:
        """Pre-seed a quality score for a known-good model.

        Sets quality_score to 100.0 (maximum), so that known-good static models
        always outrank unknown catalog entries (which use the adaptive floor of ~40).
        A single real successful event immediately overwrites the pre-seed via
        _recompute_score, so a broken or slow model drops to its true score right
        away. The pre-seed therefore acts as a trusted-foundation signal that is
        safe to override — it never permanently locks a model into the top tier.
        """
        with self._db.connection() as conn:
            if self._db.is_postgres:
                conn.execute(
                    "INSERT INTO model_quality (provider, model, quality_score, "
                    "total_calls, success_calls) VALUES (?, ?, 100.0, 1, 1) "
                    "ON CONFLICT(provider, model) DO UPDATE SET quality_score = 100.0",
                    (provider, model),
                )
            else:
                # SQLite: INSERT OR IGNORE avoids clobbering real event data;
                # UPDATE then applies the pre-seed score on top.
                conn.execute(
                    "INSERT OR IGNORE INTO model_quality "
                    "(provider, model, quality_score, total_calls, success_calls) "
                    "VALUES (?, ?, 100.0, 1, 1)",
                    (provider, model),
                )
                conn.execute(
                    "UPDATE model_quality SET quality_score = 100.0 "
                    "WHERE provider = ? AND model = ?",
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
        """Recompute quality_score = success_rate * 80 + latency_bonus * 20.

        Both factors are computed over the most recent events (a sliding
        window), so old failures stop weighting the score once enough new
        successes arrive. Lifetime totals in ``model_quality`` remain
        observable but do not gate the score.
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
        # latency_bonus: 1.0 at 0ms, 0.5 at 1000ms, exp decay
        import math
        latency_bonus = math.exp(-avg_latency / 1500.0)
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

    def rank(
        self,
        candidates: list[tuple[str, str]],
        *,
        default_score: float | None = None,
    ) -> list[tuple[str, str, float]]:
        """Sort (provider, model) candidates by quality score (highest first).

        Candidates with no recorded data use an adaptive floor computed
        from the median score of the pool, clamped to a minimum of 20.0.
        """
        scored: list[tuple[str, str, float]] = []
        known_scores: list[float] = []
        for provider, model in candidates:
            q = self.get_quality(provider, model)
            if q is not None:
                known_scores.append(q)
                scored.append((provider, model, q))
            else:
                scored.append((provider, model, -1.0))  # placeholder
        if known_scores:
            known_scores.sort()
            n = len(known_scores)
            median = known_scores[n // 2] if n % 2 else (known_scores[n // 2 - 1] + known_scores[n // 2]) / 2.0
            floor = max(20.0, median - 20.0)
        else:
            floor = 50.0  # no data at all — keep legacy default
        if default_score is not None:
            floor = default_score
        # Replace placeholders with floor
        result = [(p, m, s if s >= 0 else floor) for p, m, s in scored]
        result.sort(key=lambda x: x[2], reverse=True)
        logger.debug('ranked %d candidates', len(result))
        return result

    def status(self) -> dict[str, Any]:
        """Return summary status for /status endpoint."""
        with self._db.connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM model_quality").fetchone()[0]
            healthy = conn.execute(
                "SELECT COUNT(*) FROM model_quality WHERE quality_score >= 50.0"
            ).fetchone()[0]
        return {"total_models": count, "healthy_models": healthy}
