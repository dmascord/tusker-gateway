"""Tests for the QualityDB."""
from __future__ import annotations

import os
import tempfile

from tusker_gateway.quality import QualityDB


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
        
        # Record failures
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
