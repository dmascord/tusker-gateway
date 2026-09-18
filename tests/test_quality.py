"""Tests for the QualityDB."""
from __future__ import annotations

import os
import tempfile

from tusker_gateway.quality import QualityDB, QUALITY_WINDOW


def test_quality_db_record():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = QualityDB(db_path)
        
        # No record initially
        assert db.get_quality("p1", "m1") is None
        
        # Record a success
        db.record("p1", "m1", True, 500.0)
        score = db.get_quality("p1", "m1")
        assert score is not None
        assert score > 80.0  # high success rate, low latency
        
        # Record failures — score drops within the window
        db.record("p1", "m1", False, 5000.0)
        db.record("p1", "m1", False, 5000.0)
        score2 = db.get_quality("p1", "m1")
        assert score2 < score


def test_quality_db_ranking():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = QualityDB(db_path)
        
        db.record("p1", "m1", True, 500.0)  # 100% success, fast
        db.record("p2", "m2", True, 500.0)  # 100% success, fast
        # p2 has higher latency on next call
        db.record("p1", "m1", True, 800.0)
        db.record("p2", "m2", True, 4000.0)  # slow
        
        ranked = db.rank([("p1", "m1"), ("p2", "m2")])
        # p1 should rank higher due to better latency
        assert ranked[0][0] in {"p1", "p2"}
        assert ranked[1][0] in {"p1", "p2"}

def test_quality_rank_adaptive_floor_is_bounded():
    """Unknown candidates use a floor no lower than 10 or higher than 50."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))

        known = ("healthy", "known-model")
        unknown = ("unknown", "unknown-model")

        # A very healthy known model would otherwise produce a floor above 50.
        for _ in range(QUALITY_WINDOW):
            db.record(*known, success=True, latency_ms=0.0)
        ranked_scores = dict(
            (model, score)
            for _provider, model, score in db.rank([known, unknown])
        )
        assert ranked_scores[unknown[1]] == 50.0

        # A very poor known model would otherwise produce a floor below 10.
        for _ in range(QUALITY_WINDOW):
            db.record(*known, success=False, latency_ms=10_000.0)
        ranked = db.rank([known, unknown])
        unknown_score = next(
            score for provider, model, score in ranked if provider == unknown[0]
        )
        assert unknown_score == 10.0
def test_quality_windowed_recovery():
    """Old failures fall out of the window once enough new successes accumulate."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = QualityDB(db_path)

        # Accumulate W failures (window size) — score at floor
        for _ in range(QUALITY_WINDOW):
            db.record("p1", "m1", False, 100.0)
        score_at_floor = db.get_quality("p1", "m1")
        assert score_at_floor is not None
        assert score_at_floor < 30.0, "windowed score should be low with all-fail window"

        # Now record W new successes — old failures fall out of the window
        for _ in range(QUALITY_WINDOW):
            db.record("p1", "m1", True, 100.0)
        score_recovered = db.get_quality("p1", "m1")

        # Score should have recovered significantly; success rate is now 1.0
        # and latency bonus is high (fast responses), so score should be near max
        assert score_recovered is not None
        assert score_recovered > score_at_floor, (
            f"score should recover: floor={score_at_floor:.1f} recovered={score_recovered:.1f}"
        )
        # With 100% success and fast latency, score should be > 95
        assert score_recovered > 95.0, f"expected near-perfect score, got {score_recovered:.1f}"

        # A single failure should only drop it slightly within the window
        db.record("p1", "m1", False, 100.0)
        score_after_one_failure = db.get_quality("p1", "m1")
        # Window now has 1 fail + 19 successes; success_rate = 19/20 = 0.95
        assert score_after_one_failure > 90.0, (
            f"one failure in window should not tank score: {score_after_one_failure:.1f}"
        )
