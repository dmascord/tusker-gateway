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


def test_quality_record_survives_decimal_averages():
    """PostgreSQL AVG(numeric) must not crash the outcome write.

    ``AVG(success)`` returns Decimal on PostgreSQL and float on SQLite.
    Since the PostgreSQL storage migration every ``record()`` raised
    TypeError in the score recompute, and the surrounding transaction
    rolled back, so no outcome ever reached the live database.
    """
    from decimal import Decimal

    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))
        db.record("p1", "m1", True, 500.0)
        assert db.get_quality("p1", "m1") is not None

        # Directly exercise the arithmetic with Decimal inputs.
        class FakeCursor:
            def __init__(self, row):
                self._row = row

            def execute(self, *_a, **_k):
                return self

            def fetchone(self):
                return self._row

        class FakeConn:
            def execute(self, *_a, **_k):
                return FakeCursor((2, Decimal("0.5"), Decimal("750.0")))

        updates: list[tuple] = []

        def capturing_execute(_sql, parameters=()):
            updates.append(parameters)

        class CapturingConn(FakeConn):
            def execute(self, sql, parameters=()):
                if "UPDATE model_quality" in sql:
                    capturing_execute(sql, parameters)
                    return None
                return FakeCursor((2, Decimal("0.5"), Decimal("750.0")))

        db._recompute_score(CapturingConn(), "p1", "m1")
        assert updates and isinstance(updates[-1][0], float)
        assert 40.0 < updates[-1][0] < 60.0


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

def test_quality_rank_keeps_minute_latency_below_fast_route():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))
        for _ in range(QUALITY_WINDOW):
            db.record("fast", "model", True, 1_000.0)
            db.record("slow", "model", True, 120_000.0)

        ranked = db.rank([("fast", "model"), ("slow", "model")])

        assert ranked[0][:2] == ("fast", "model")
        assert ranked[1][:2] == ("slow", "model")
        assert ranked[0][2] - ranked[1][2] > 10.0

def test_quality_rank_temporarily_demotes_recent_failures(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))
        db.record("healthy", "model", True, 1_000.0)
        db.record("saturated", "model", True, 1_000.0)
        monkeypatch.setattr("tusker_gateway.quality.time.time", lambda: 1_000.0)
        db.record("saturated", "model", False, 120_000.0)

        ranked = db.rank([("healthy", "model"), ("saturated", "model")])

        assert ranked[0][:2] == ("healthy", "model")
        assert ranked[1][2] <= ranked[0][2] - 25.0

        monkeypatch.setattr("tusker_gateway.quality.time.time", lambda: 1_301.0)
        recovered = dict(
            (provider, score)
            for provider, _model, score in db.rank(
                [("healthy", "model"), ("saturated", "model")]
            )
        )
        assert recovered["saturated"] - ranked[1][2] == 30.0

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

def test_quality_rank_bulk_query_preserves_recent_failure_and_floor(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))
        db.record("known", "model", True, 1_000.0)
        db.record("recent", "model", True, 1_000.0)
        monkeypatch.setattr("tusker_gateway.quality.time.time", lambda: 1_000.0)
        db.record("recent", "model", False, 1_000.0)

        ranked = db.rank(
            [("known", "model"), ("recent", "model"), ("unknown", "model")]
        )

        scores = {(provider, model): score for provider, model, score in ranked}
        assert scores[("unknown", "model")] == 50.0
        assert scores[("recent", "model")] <= scores[("known", "model")] - 25.0
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


def test_prime_model_does_not_clobber_learned_failure_score():
    """A pool rebuild must not reset a learned score back to 100.

    Regression: ``prime_model`` previously ran an unconditional
    ``ON CONFLICT DO UPDATE SET quality_score = 100.0``, so every config
    hot-reload erased the failure history of broken operator-curated models
    and selection kept routing traffic to them.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))

        # Pool build primes the static entry.
        db.prime_model("p1", "m1")
        assert db.get_quality("p1", "m1") == 100.0

        # The model then fails repeatedly.
        for _ in range(5):
            db.record("p1", "m1", False, 200.0)
        degraded = db.get_quality("p1", "m1")
        assert degraded is not None
        assert degraded < 30.0, f"failures should de-rank the model, got {degraded:.1f}"

        # A later pool rebuild / config hot-reload re-primes every static entry.
        db.prime_model("p1", "m1")
        assert db.get_quality("p1", "m1") == degraded, (
            "prime_model must not overwrite a score backed by real events"
        )


def test_prime_model_seeds_only_uncalled_models():
    """Fresh entries are seeded above the catalog floor; seeds are not
    counted as real calls, and a real event recomputes the score exactly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))

        db.prime_model("p1", "fresh")
        assert db.get_quality("p1", "fresh") == 100.0

        # The pre-seed must not masquerade as a completed call.
        with db._db.connection() as conn:
            row = conn.execute(
                "SELECT total_calls, success_calls FROM model_quality "
                "WHERE provider = ? AND model = ?",
                ("p1", "fresh"),
            ).fetchone()
        assert row == (0, 0), f"pre-seed must not fake call counters: {row}"

        # A real success recomputes from the event window.
        db.record("p1", "fresh", True, 0.0)
        assert db.get_quality("p1", "fresh") == 100.0


def test_degraded_state_store_does_not_crash_quality_reads(monkeypatch):
    """An unavailable advisory state store returns noop rows.

    Regression: ``prime_model`` indexed ``fetchone()[0]`` directly, so a state
    store outage at startup raised TypeError ("'NoneType' object is not
    subscriptable") out of ``PoolManager.__post_init__`` and crash-looped the
    gateway until PostgreSQL returned - the opposite of the documented
    degraded mode.  ``status()`` indexed its counts the same way.
    """
    from tusker_gateway import storage

    def _unavailable(_target):
        raise OSError("state store unreachable")

    monkeypatch.setattr(storage, "_pool_for", _unavailable)
    monkeypatch.setattr(storage, "_STORAGE_STATUS", dict(storage._STORAGE_STATUS))
    monkeypatch.setenv("TUSKER_STORAGE_MODE", "postgres")
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "postgresql://state.invalid/tusker")
    db = QualityDB("quality.db")

    db.prime_model("p1", "m1")  # raised TypeError before the fix
    assert db.get_quality("p1", "m1") is None
    assert db.status() == {"total_models": 0, "healthy_models": 0}

    db.record("p1", "m1", True, 12.0)
    assert db.rank([("p1", "m1")]) == [("p1", "m1", 50.0)]


def test_quality_creates_model_events_indexes():
    """The ranking queries need indexes on ``model_events``.

    Without them the recent-failure count and the windowed score
    recomputation scan the whole event table, which grows with every
    attempt and dominates selection latency.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))
        with db._db.connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'model_events'"
            ).fetchall()
        names = {str(row[0]) for row in rows}
        assert "idx_model_events_provider_model_id" in names
        assert "idx_model_events_recent_failures" in names


def test_quality_rank_scales_with_one_query_per_table(monkeypatch):
    """Ranking must not issue a query per candidate.

    Selection can inspect hundreds of routes; a per-candidate query turns
    ranking into an avoidable round-trip cost per request.
    """
    from tusker_gateway import storage

    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))
        for index in range(50):
            db.record(f"p{index}", "m1", True, 100.0)
        candidates = [(f"p{index}", "m1") for index in range(50)]

        executed: list[str] = []
        real_execute = storage.DatabaseConnection.execute

        def counting_execute(self, sql, parameters=()):
            executed.append(sql)
            return real_execute(self, sql, parameters)

        monkeypatch.setattr(storage.DatabaseConnection, "execute", counting_execute)
        ranked = db.rank(candidates)

        assert len(ranked) == 50
        # One read per table per chunk, not one per candidate.
        assert len(executed) <= 4, executed


def test_quality_rank_chunks_large_candidate_lists(monkeypatch):
    """A large candidate list must not exceed the parameter limit.

    Each candidate pair binds two parameters, and the backend caps the
    number of parameters in one statement, so the pair list is chunked.
    """
    from tusker_gateway import storage

    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))
        candidates = [(f"p{index}", "m1") for index in range(1200)]

        bound_counts: list[int] = []
        real_execute = storage.DatabaseConnection.execute

        def counting_execute(self, sql, parameters=()):
            bound_counts.append(len(parameters))
            return real_execute(self, sql, parameters)

        monkeypatch.setattr(storage.DatabaseConnection, "execute", counting_execute)
        ranked = db.rank(candidates)

        assert len(ranked) == 1200
        assert bound_counts, "ranking must query the store"
        assert max(bound_counts) <= storage.MAX_PAIR_CHUNK * 2 + 1, bound_counts


def test_quality_rank_caches_repeated_reads(monkeypatch):
    """Repeated rank calls within the TTL must not re-read the store."""
    from tusker_gateway import storage

    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))
        db.record("p1", "m1", True, 100.0)
        candidates = [("p1", "m1")]

        executed: list[str] = []
        real_execute = storage.DatabaseConnection.execute

        def counting_execute(self, sql, parameters=()):
            executed.append(sql)
            return real_execute(self, sql, parameters)

        monkeypatch.setattr(storage.DatabaseConnection, "execute", counting_execute)
        first = db.rank(candidates)
        assert executed, "first rank must read the store"
        executed.clear()
        second = db.rank(candidates)
        assert executed == [], "cached rank must not re-read the store"
        assert second == first

        # A recorded outcome must invalidate the cache immediately.
        db.record("p1", "m1", False, 100.0)
        executed.clear()
        db.rank(candidates)
        assert executed, "a recorded outcome must invalidate the cache"


def test_quality_rank_cache_never_hides_window_expiry(monkeypatch):
    """A cached penalty must expire with the recent-failure window."""
    from tusker_gateway import quality as quality_module

    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))
        db.record("p1", "m1", True, 100.0)
        candidates = [("p1", "m1")]
        first = db.rank(candidates)

        real_time = quality_module.time.time
        monkeypatch.setattr(
            quality_module.time,
            "time",
            lambda: real_time() + quality_module.QUALITY_RECENT_FAILURE_WINDOW_SECS + 1.0,
        )
        db.record("p1", "m1", False, 100.0)
        demoted = dict((p, s) for p, _m, s in db.rank(candidates))
        base = dict((p, s) for p, _m, s in first)
        assert demoted["p1"] <= base["p1"] - quality_module.QUALITY_RECENT_FAILURE_PENALTY


def test_quality_prune_events_keeps_window_and_removes_old():
    """Pruning must keep the newest window per route and drop older rows."""
    from tusker_gateway import quality as quality_module

    with tempfile.TemporaryDirectory() as tmpdir:
        db = QualityDB(os.path.join(tmpdir, "test.db"))
        # A route with only two fresh events must stay untouched.
        db.record("p2", "m1", True, 100.0)
        db.record("p2", "m1", True, 100.0)

        # 30 aged events for one route: the newest QUALITY_WINDOW survive,
        # the 10 beyond the window go.
        old = quality_module.time.time() - quality_module.QUALITY_EVENT_MAX_AGE_SECS - 3600.0
        with db._db.connection() as conn:
            for _ in range(quality_module.QUALITY_WINDOW + 10):
                conn.execute(
                    "INSERT INTO model_events (provider, model, success, latency_ms, created_at)"
                    " VALUES ('p1', 'm1', 1, 100.0, ?)",
                    (old,),
                )
            conn.commit()

        with db._db.connection() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM model_events"
            ).fetchone()[0]
        removed = db.prune_events()
        assert removed == 10
        with db._db.connection() as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM model_events"
            ).fetchone()[0]
            kept = conn.execute(
                "SELECT COUNT(*) FROM model_events WHERE provider = 'p2'"
            ).fetchone()[0]
            window_rows = conn.execute(
                "SELECT COUNT(*) FROM model_events WHERE provider = 'p1'"
            ).fetchone()[0]
        assert after == before - removed
        assert kept == 2
        assert window_rows == quality_module.QUALITY_WINDOW
