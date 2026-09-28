"""Tests for LLM Stats capability rankings: sync, verdicts, and pool filter."""
from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest

from tusker_gateway.model_rankings import (
    ModelRankingsDB,
    load_llm_stats_env_config,
    normalize_slug,
    pool_model_pairs,
    sync_llm_stats_rankings,
)
from tusker_gateway.config import PoolConfig
from tusker_gateway.pools import PoolManager
from tusker_gateway.quality import QualityDB


# ---------------------------------------------------------------------------
# normalize_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-oss:20b", "gpt-oss-20b"),
        ("MiniMax-M3", "minimax-m3"),
        ("hf:openai/gpt-oss-120b", "gpt-oss-120b"),
        ("openai/gpt-oss-20b", "gpt-oss-20b"),
        ("hf:moonshotai/Kimi-K3", "kimi-k3"),
        ("hf:Qwen/Qwen3.8-27B", "qwen3.8-27b"),
        ("syn:large:text", "syn-large-text"),
        ("ollama/deepseek-v4-flash", "deepseek-v4-flash"),
        ("", ""),
    ],
)
def test_normalize_slug(model: str, expected: str):
    assert normalize_slug(model) == expected


# ---------------------------------------------------------------------------
# Verdict DB round trip
# ---------------------------------------------------------------------------


def _verdict(slug: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "slug": slug,
        "status": status,
        "category_rank": extra.get("category_rank"),
        "category_name": extra.get("category_name"),
        "evidence": extra.get("evidence", "test"),
        "synced_at": extra.get("synced_at", time.time()),
    }


def test_verdict_db_round_trip_and_fail_open(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    assert db.excludes("gpt-oss:20b") is False  # no data yet

    db.replace_all(
        [
            _verdict("gpt-oss-20b", "excluded", category_rank=41, category_name="general"),
            _verdict("minimax-m3", "pass", category_rank=46, evidence="category_window"),
            _verdict("syn-large-text", "unknown", evidence="not_in_llm_stats"),
        ],
        time.time(),
    )

    assert db.excludes("ollama-cloud gpt-oss:20b".replace(" ", "/")) is True
    assert db.excludes("MiniMax-M3") is False
    assert db.excludes("syn:large:text") is False  # unknown fails open
    assert db.excludes("totally-unknown-model") is False

    status = db.status()
    assert status["models"] == 3
    assert status["statuses"] == {"excluded": 1, "pass": 1, "unknown": 1}

    # replace_all drops stale slugs
    db.replace_all([_verdict("minimax-m3", "pass")], time.time())
    assert db.excludes("gpt-oss:20b") is False
    assert db.status()["models"] == 1


# ---------------------------------------------------------------------------
# Sync with a fake HTTP session
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, body: Any):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeSession:
    """Routes URLs to canned responses and counts requests."""

    def __init__(self, routes: dict[str, _Response]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        response = self.routes.get(url)
        if response is None:
            response = _Response(404, {})
        return response


def _config(tmp_path, **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "llm_stats_db_path": str(tmp_path / "llm_stats.db"),
        "llm_stats_api_key": "test-key",
        "llm_stats_base_url": "https://llmstats.test/stats/v1",
        "llm_stats_categories": ("code", "general", "reasoning"),
        "llm_stats_max_rank": 25,
        "llm_stats_pacing_secs": 0,
    }
    config.update(overrides)
    return config


def _rankings_body(entries: list[tuple[str, int]]) -> dict[str, Any]:
    return {
        "category": "code",
        "method": "trueskill",
        "models": [
            {"rank": rank, "model_id": model_id} for model_id, rank in entries
        ],
    }


@pytest.mark.asyncio
async def test_sync_window_verdicts_pass_excluded_unknown(tmp_path):
    base = "https://llmstats.test/stats/v1"
    routes = {
        f"{base}/rankings?category=agents&limit=50": _Response(200, _rankings_body([])),
        f"{base}/rankings?category=code&limit=50": _Response(
            200, _rankings_body([("minimax-m3", 46)])
        ),
        f"{base}/rankings?category=general&limit=50": _Response(200, _rankings_body([])),
        # syn-large-text: absent from every tracked window.
    }
    session = _FakeSession(routes)
    config = _config(tmp_path)
    pairs = [
        ("minimax", "MiniMax-M3"),
        ("synthetic", "syn:large:text"),
    ]

    summary = await sync_llm_stats_rankings(config, pairs, session)

    assert summary["excluded"] == 1
    assert summary["unknown"] == 1
    assert summary["passed"] == 0
    assert summary["rate_limited"] is False
    assert summary["requests"] == 3  # windows only

    db = ModelRankingsDB(config["llm_stats_db_path"])
    rows = db.rows()
    assert rows["minimax-m3"]["status"] == "excluded"
    assert rows["minimax-m3"]["category_rank"] == 46
    assert rows["minimax-m3"]["evidence"] == "category_window"
    assert rows["syn-large-text"]["status"] == "unknown"
    assert db.excludes("MiniMax-M3") is True
    assert db.excludes("syn:large:text") is False


@pytest.mark.asyncio
async def test_sync_takes_best_rank_across_tracked_categories(tmp_path):
    base = "https://llmstats.test/stats/v1"
    routes = {
        # The agents rank (40, worse than cutoff) must not hide the code
        # rank (5); the best tracked position decides the verdict.
        f"{base}/rankings?category=agents&limit=50": _Response(
            200, _rankings_body([("minimax-m3", 40)])
        ),
        f"{base}/rankings?category=code&limit=50": _Response(
            200, _rankings_body([("minimax-m3", 5)])
        ),
        f"{base}/rankings?category=general&limit=50": _Response(
            200, _rankings_body([])
        ),
    }
    session = _FakeSession(routes)

    summary = await sync_llm_stats_rankings(
        _config(tmp_path), [("minimax", "MiniMax-M3")], session
    )

    assert summary["passed"] == 1
    row = ModelRankingsDB(_config(tmp_path)["llm_stats_db_path"]).rows()["minimax-m3"]
    assert row["status"] == "pass"
    assert row["category_rank"] == 5
    assert row["category_name"] == "code"


@pytest.mark.asyncio
async def test_sync_window_hit_within_cutoff_passes(tmp_path):
    base = "https://llmstats.test/stats/v1"
    routes = {
        f"{base}/rankings?category=code&limit=50": _Response(
            200, _rankings_body([("kimi-k3", 3)])
        ),
        f"{base}/rankings?category=general&limit=50": _Response(
            200, _rankings_body([])
        ),
    }
    session = _FakeSession(routes)

    summary = await sync_llm_stats_rankings(
        _config(tmp_path), [("moonshot", "hf:moonshotai/Kimi-K3")], session
    )

    assert summary["passed"] == 1
    assert summary["requests"] == 3
    row = ModelRankingsDB(_config(tmp_path)["llm_stats_db_path"]).rows()["kimi-k3"]
    assert row["status"] == "pass"
    assert row["category_rank"] == 3
    assert row["category_name"] == "code"
    assert row["evidence"] == "category_window"


@pytest.mark.asyncio
async def test_sync_429_keeps_previous_verdicts(tmp_path):
    base = "https://llmstats.test/stats/v1"
    db_path = str(tmp_path / "llm_stats.db")
    seed = ModelRankingsDB(db_path)
    seed.replace_all([_verdict("minimax-m3", "pass")], time.time())

    routes = {
        # First window fine, second window rate-limits.
        f"{base}/rankings?category=agents&limit=50": _Response(200, _rankings_body([])),
        f"{base}/rankings?category=code&limit=50": _Response(429, None),
        f"{base}/rankings?category=general&limit=50": _Response(200, _rankings_body([])),
    }
    session = _FakeSession(routes)
    summary = await sync_llm_stats_rankings(
        _config(tmp_path, llm_stats_db_path=db_path),
        [("minimax", "MiniMax-M3")],
        session,
    )

    assert summary["rate_limited"] is True
    rows = seed.rows()
    assert rows["minimax-m3"]["status"] == "pass"  # untouched


@pytest.mark.asyncio
async def test_sync_requires_api_key(tmp_path):
    session = _FakeSession({})
    with pytest.raises(ValueError):
        await sync_llm_stats_rankings(
            _config(tmp_path, llm_stats_api_key=""), [("p", "m")], session
        )

@pytest.mark.asyncio
async def test_sync_preserves_verdicts_outside_pair_set(tmp_path):
    """A narrow sync must not delete verdicts for models it was not asked about.

    The caller's pair set is config dependent: the gateway's refresh loop sees
    auto-catalog-expanded pools, while a one-shot process may see only the
    env-fallback pools. Swapping the whole table in the narrow case would drop
    valid exclusions and silently make weak models selectable again.
    """
    base = "https://llmstats.test/stats/v1"
    db_path = str(tmp_path / "llm_stats.db")
    db = ModelRankingsDB(db_path)
    stale_synced_at = time.time() - 3600
    db.replace_all(
        [
            _verdict("minimax-m3", "pass"),
            _verdict(
                "legacy-model",
                "excluded",
                category_rank=46,
                category_name="code",
                evidence="category_window",
                synced_at=stale_synced_at,
            ),
        ],
        time.time(),
    )

    routes = {
        f"{base}/rankings?category=agents&limit=50": _Response(200, _rankings_body([])),
        f"{base}/rankings?category=code&limit=50": _Response(
            200, _rankings_body([("minimax-m3", 46)])
        ),
        f"{base}/rankings?category=general&limit=50": _Response(200, _rankings_body([])),
    }
    summary = await sync_llm_stats_rankings(
        _config(tmp_path, llm_stats_db_path=db_path),
        [("minimax", "MiniMax-M3")],
        session=_FakeSession(routes),
    )

    assert summary["preserved"] == 1
    rows = db.rows()
    # The model in scope was refreshed in place.
    assert rows["minimax-m3"]["status"] == "excluded"
    # The out-of-scope verdict survived untouched, including its sync time.
    assert rows["legacy-model"]["status"] == "excluded"
    assert rows["legacy-model"]["category_rank"] == 46
    assert rows["legacy-model"]["synced_at"] == pytest.approx(stale_synced_at)
    assert db.excludes("legacy-model") is True


def test_pool_model_pairs_reads_pool_configs():
    config = {
        "pools": {
            "code": PoolConfig(
                name="code",
                models=[
                    {"provider": "groq", "model": "openai/gpt-oss-20b"},
                    {"provider": "xiaomi", "model": "mimo-v2.5"},
                ],
            ),
            "privacy": PoolConfig(name="privacy", models=[]),
        }
    }
    assert pool_model_pairs(config) == [
        ("groq", "openai/gpt-oss-20b"),
        ("xiaomi", "mimo-v2.5"),
    ]


# ---------------------------------------------------------------------------
# Pool filter integration
# ---------------------------------------------------------------------------


def _pool_manager(
    tmpdir: str, models: list[dict[str, str]], **extra: Any
) -> PoolManager:
    config: dict[str, Any] = {
        "pools": {
            "code": PoolConfig(name="code", models=models),
        },
        "quality_db_path": os.path.join(tmpdir, "quality.db"),
        "llm_stats_db_path": os.path.join(tmpdir, "llm_stats.db"),
        "excluded_providers": [],
        "provider_api_keys": {"ollama-cloud": "k1", "xiaomi": "k2"},
    }
    config.update(extra)
    return PoolManager(config)


def test_pool_filter_drops_excluded_model_and_stickiness(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [
            _verdict("gpt-oss-20b", "excluded", category_rank=41, category_name="general"),
            _verdict("mimo-v2-5", "pass", category_rank=1, category_name="agents"),
        ],
        time.time(),
    )

    manager = _pool_manager(
        str(tmp_path),
        [
            {"provider": "ollama-cloud", "model": "gpt-oss:20b"},
            {"provider": "xiaomi", "model": "mimo-v2.5"},
        ],
    )

    # gpt-oss:20b is excluded; selection falls to mimo-v2.5.
    assert manager.select("code") == ("xiaomi", "mimo-v2.5")

    # A session pinned to the now-excluded model is invalidated.
    manager._remember_stickiness(("sticky", "code"), ("ollama-cloud", "gpt-oss:20b"))
    assert manager.select("code", session_id="sticky") == ("xiaomi", "mimo-v2.5")


def test_pool_filter_unknown_and_pass_models_stay_selectable(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [
            _verdict("gpt-oss-120b", "unknown", evidence="not_in_llm_stats"),
            _verdict("mimo-v2-5", "pass", category_rank=46, category_name="code"),
        ],
        time.time(),
    )

    manager = _pool_manager(
        str(tmp_path),
        [
            {"provider": "ollama-cloud", "model": "gpt-oss:120b"},
            {"provider": "xiaomi", "model": "mimo-v2.5"},
        ],
    )

    selected = {manager.select("code") for _ in range(4)}
    assert selected == {("ollama-cloud", "gpt-oss:120b"), ("xiaomi", "mimo-v2.5")}


# ---------------------------------------------------------------------------
# Rank enforcement modes
# ---------------------------------------------------------------------------


def test_prefer_enforcement_keeps_excluded_model_and_rotates_to_it(tmp_path):
    """Rank orders rotation instead of gating it (enforcement=prefer).

    The stronger model wins while it is healthy; when it cools down the
    out-of-cutoff model takes over rather than the request failing.
    """
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [
            _verdict("mimo-v2-5", "pass", category_rank=1, category_name="agents"),
            _verdict(
                "gpt-oss-20b",
                "excluded",
                category_rank=191,
                category_name="tool_calling",
            ),
        ],
        time.time(),
    )
    manager = _pool_manager(
        str(tmp_path),
        [
            {"provider": "xiaomi", "model": "mimo-v2.5"},
            {"provider": "ollama-cloud", "model": "gpt-oss:20b"},
        ],
        llm_stats_enforcement="prefer",
    )

    assert manager.select("code") == ("xiaomi", "mimo-v2.5")
    manager._cooldowns.cooldown("xiaomi", "mimo-v2.5", 30)
    assert manager.select("code") == ("ollama-cloud", "gpt-oss:20b")


def test_drop_enforcement_removes_sole_excluded_candidate(tmp_path):
    """The default mode still treats an out-of-cutoff model as a blacklist."""
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [_verdict("gpt-oss-20b", "excluded", category_rank=41, category_name="general")],
        time.time(),
    )
    manager = _pool_manager(
        str(tmp_path), [{"provider": "ollama-cloud", "model": "gpt-oss:20b"}]
    )

    assert manager.select("code") is None


def test_prefer_enforcement_defaults_span_the_rank_ladder(monkeypatch, tmp_path):
    """Prefer mode must keep a rank gradient past the drop-mode cutoff.

    The drop-mode decay reaches zero at rank ~26, which would leave every
    weaker model with an identical bonus and no ordering to fall through.
    """
    monkeypatch.delenv("TUSKER_LLM_STATS_RANK_BOOST_PER_RANK", raising=False)

    monkeypatch.delenv("TUSKER_LLM_STATS_ENFORCEMENT", raising=False)
    drop_config: dict[str, Any] = {"quality_db_path": str(tmp_path / "quality.db")}
    load_llm_stats_env_config(drop_config)
    assert drop_config["llm_stats_enforcement"] == "drop"
    assert drop_config["llm_stats_rank_boost_per_rank"] == 1.0

    monkeypatch.setenv("TUSKER_LLM_STATS_ENFORCEMENT", "prefer")
    prefer_config: dict[str, Any] = {"quality_db_path": str(tmp_path / "quality.db")}
    load_llm_stats_env_config(prefer_config)
    assert prefer_config["llm_stats_enforcement"] == "prefer"

    manager = _pool_manager(str(tmp_path), [], **prefer_config)
    assert manager._rank_boost(150) > 0.0
    # Monotone: better ranks keep a strictly larger bonus across the range.
    assert manager._rank_boost(1) > manager._rank_boost(100) > manager._rank_boost(150)


# ---------------------------------------------------------------------------
# Rank-preferring selection
# ---------------------------------------------------------------------------


def test_effective_rank_uses_window_category_rank(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [
            _verdict("alpha", "pass", category_rank=9, category_name="code"),
            _verdict("delta", "pass"),  # no window presence
            _verdict("gamma", "unknown"),
        ],
        time.time(),
    )
    assert db.effective_rank("alpha") == 9
    assert db.effective_rank("delta") is None  # no window rank: no bonus
    assert db.effective_rank("gamma") is None
    assert db.effective_rank("not-tracked") is None


def test_rank_boost_is_bounded_and_decays():
    manager = PoolManager({"pools": {}, "quality_db_path": ":memory:"})
    assert manager._rank_boost(1) == 25.0
    assert manager._rank_boost(10) == 16.0
    assert manager._rank_boost(25) == 1.0
    assert manager._rank_boost(26) == 0.0
    assert manager._rank_boost(0) == 0.0


def test_pool_selection_prefers_better_llm_stats_rank(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [
            _verdict("mimo-v2-5", "pass", category_rank=20, category_name="code"),
            _verdict("gpt-oss-120b", "pass", category_rank=3, category_name="code"),
        ],
        time.time(),
    )
    manager = _pool_manager(
        str(tmp_path),
        [
            {"provider": "xiaomi", "model": "mimo-v2.5"},
            {"provider": "ollama-cloud", "model": "gpt-oss:120b"},
        ],
    )

    # Learned quality is identical (both primed to the same score), so the
    # better rank decides every call instead of round-robin distribution.
    for _ in range(4):
        assert manager.select("code") == ("ollama-cloud", "gpt-oss:120b")


def test_rank_boost_does_not_rescue_a_failing_model(tmp_path):
    quality_path = os.path.join(str(tmp_path), "quality.db")
    quality = QualityDB(quality_path)
    for _ in range(10):
        quality.record("ollama-cloud", "gpt-oss:120b", False, 50.0)

    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all([_verdict("gpt-oss-120b", "pass", category_rank=1)], time.time())

    manager = _pool_manager(
        str(tmp_path),
        [
            {"provider": "ollama-cloud", "model": "gpt-oss:120b"},
            {"provider": "xiaomi", "model": "mimo-v2.5"},
        ],
    )

    # The rank-1 model has 0% learned success: even with the maximum bonus
    # its score stays below the healthy model's, so rank cannot resurrect it.
    assert manager.select("code") == ("xiaomi", "mimo-v2.5")


def test_rank_boost_disabled_restores_round_robin(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [
            _verdict("mimo-v2-5", "pass", category_rank=20),
            _verdict("gpt-oss-120b", "pass", category_rank=1),
        ],
        time.time(),
    )
    manager = PoolManager(
        {
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[
                        {"provider": "xiaomi", "model": "mimo-v2.5"},
                        {"provider": "ollama-cloud", "model": "gpt-oss:120b"},
                    ],
                )
            },
            "quality_db_path": os.path.join(str(tmp_path), "quality.db"),
            "llm_stats_db_path": os.path.join(str(tmp_path), "llm_stats.db"),
            "excluded_providers": [],
            "provider_api_keys": {"ollama-cloud": "k1", "xiaomi": "k2"},
            "llm_stats_rank_boost_per_rank": 0.0,
        }
    )

    selected = {manager.select("code") for _ in range(4)}
    assert selected == {("xiaomi", "mimo-v2.5"), ("ollama-cloud", "gpt-oss:120b")}


# ---------------------------------------------------------------------------
# Website-seed extraction and matching
# ---------------------------------------------------------------------------


from tusker_gateway.model_rankings import (
    MATCH_MIN_OVERLAP,
    SEED_EVIDENCE,
    extract_leaderboard_arrays,
    match_arrays_to_windows,
)
from tusker_gateway.tools.import_llm_stats_seed import build_seed_rows


def _escaped_payload(*parts: str) -> str:
    """Wrap plain JSON ``parts`` inside the Next.js ``__next_f`` escape."""
    inner = "".join(parts)
    return (
        'self.__next_f.push([1,"' + inner.replace('"', '\\"') + '"])'
    )


def test_extract_leaderboard_arrays_parses_named_category_keys():
    html = _escaped_payload(
        '{"initialAllIndexes":{',
        '"code":{"category_id":"code","models":['
        '{"model_id":"a","name":"A","rank":1},'
        '{"model_id":"b","name":"B","rank":2}]},',
        '"general":{"category_id":"general","models":['
        '{"model_id":"a","name":"A","rank":1},'
        '{"model_id":"x","name":"X","rank":2}]},',
        '}}',
    )
    arrays = extract_leaderboard_arrays(html)
    assert sorted(k for k, _ in arrays) == ["code", "general"]
    code = next(entries for k, entries in arrays if k == "code")
    assert code == [("a", 1), ("b", 2)]
    general = next(entries for k, entries in arrays if k == "general")
    assert general == [("a", 1), ("x", 2)]


def test_extract_leaderboard_arrays_handles_bracket_inside_string():
    # Model name with a literal ``]`` must not fool the bracket counter.
    html = _escaped_payload(
        '{"initialAllIndexes":{'
        '"code":{"category_id":"code","models":['
        '{"model_id":"a","name":"A ]","rank":1},'
        '{"model_id":"b","name":"B","rank":2}'
        ']}}}',
    )
    arrays = extract_leaderboard_arrays(html)
    assert arrays == [("code", [("a", 1), ("b", 2)])]


def test_match_arrays_requires_key_identity_and_overlap():
    # Ten positions so a single drifted slot (9/10 = 0.9) meets the
    # MATCH_MIN_OVERLAP threshold; a three-slot window would drop it.
    ordered = [f"m{i}" for i in range(10)]
    window = {slug: rank + 1 for rank, slug in enumerate(ordered)}
    drifted = ordered.copy()
    drifted[9] = "zz"  # one slot drifted: 9/10 positional overlap
    arrays = [
        # Same key, one drifted position — accepted via rule 1
        ("code", [(slug, rank + 1) for rank, slug in enumerate(drifted)]),
        # Different key but exact prefix — rename-safe fallback
        ("legacy_code", [(slug, rank + 1) for rank, slug in enumerate(ordered)]),
        # Same key but entirely different ordering — must be rejected
        ("other", [("a", 1), ("x", 2), ("y", 3)]),
    ]
    matches = match_arrays_to_windows({"code": window}, arrays)
    assert matches["code"] == arrays[0][1]
    assert matches["code"] != arrays[1][1]


def test_match_arrays_rejects_same_key_with_poor_overlap():
    # Same site key but ordering drifted beyond the tolerance: the
    # matcher must reject instead of trusting the name alone.
    ordered = [f"m{i}" for i in range(10)]
    window = {slug: rank + 1 for rank, slug in enumerate(ordered)}
    poor = list(reversed(ordered))  # 0/10 positional overlap
    arrays = [("code", [(slug, rank + 1) for rank, slug in enumerate(poor)])]
    matches = match_arrays_to_windows({"code": window}, arrays)
    assert "code" not in matches


def test_match_arrays_falls_back_to_exact_prefix_when_keys_differ():
    window = {"a": 1, "b": 2, "c": 3}
    arrays = [
        ("renamed_code", [("a", 1), ("b", 2), ("c", 3)]),
    ]
    matches = match_arrays_to_windows({"code": window}, arrays)
    # No same-key candidate; the exact-prefix rename fallback accepts.
    assert matches["code"] == arrays[0][1]


# ---------------------------------------------------------------------------
# Seed persistence and sync overlay
# ---------------------------------------------------------------------------


def test_apply_seed_preserves_category_window_rows(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [_verdict("claude-opus-5-5", "pass", category_rank=2, evidence="category_window")],
        time.time(),
    )
    result = db.apply_seed(
        [
            _verdict("claude-opus-5-5", "excluded", evidence=SEED_EVIDENCE, category_rank=400),
            _verdict("gpt-oss-20b", "excluded", evidence=SEED_EVIDENCE, category_rank=222),
        ],
        time.time(),
    )
    assert result == {"written": 1, "skipped_window": 1}
    rows = db.rows()
    # Window-evidence row must remain untouched.
    assert rows["claude-opus-5-5"]["status"] == "pass"
    assert rows["claude-opus-5-5"]["evidence"] == "category_window"
    # Seeded row written.
    assert rows["gpt-oss-20b"]["status"] == "excluded"
    assert rows["gpt-oss-20b"]["evidence"] == SEED_EVIDENCE


def test_apply_seed_refreshes_existing_seed_row(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    older = time.time() - 86_400
    db.replace_all(
        [_verdict("gpt-oss-20b", "excluded", evidence=SEED_EVIDENCE, category_rank=222, synced_at=older)],
        older,
    )
    newer = time.time()
    result = db.apply_seed(
        [_verdict("gpt-oss-20b", "excluded", evidence=SEED_EVIDENCE, category_rank=180)],
        newer,
    )
    assert result == {"written": 1, "skipped_window": 0}
    assert db.rows()["gpt-oss-20b"]["category_rank"] == 180
    assert db.rows()["gpt-oss-20b"]["synced_at"] == pytest.approx(newer, abs=1e-5)


@pytest.mark.asyncio
async def test_sync_carries_forward_fresh_seed_row(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    # Pre-existing fresh seed for a model absent from the windows.
    fresh = time.time()
    db.replace_all(
        [_verdict("gpt-oss-20b", "excluded", evidence=SEED_EVIDENCE, category_rank=222, synced_at=fresh)],
        fresh,
    )
    # Routes only return a window for one tracked category that does not
    # contain gpt-oss-20b.
    base = "https://llmstats.test/stats/v1"
    routes = {
        f"{base}/rankings?category=code&limit=50": _Response(
            200, _rankings_body([("minimax-m3", 5)])
        ),
        f"{base}/rankings?category=general&limit=50": _Response(200, _rankings_body([])),
        f"{base}/rankings?category=reasoning&limit=50": _Response(200, _rankings_body([])),
    }
    config = _config(tmp_path)
    pairs = [("ollama-cloud", "gpt-oss:20b"), ("minimax", "MiniMax-M3")]
    summary = await sync_llm_stats_rankings(config, pairs, _FakeSession(routes))
    assert summary["seed_kept"] == 1
    assert summary["passed"] == 1  # minimax-m3 from window evidence
    assert summary["unknown"] == 0
    row = db.rows()["gpt-oss-20b"]
    assert row["evidence"] == SEED_EVIDENCE
    assert row["status"] == "excluded"
    # synced_at preserved from the seed so the age clock keeps ticking.
    assert row["synced_at"] == pytest.approx(fresh, abs=0.01)


@pytest.mark.asyncio
async def test_sync_degrades_stale_seed_row(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    stale = time.time() - 30 * 86_400  # well beyond 7-day default TTL
    db.replace_all(
        [_verdict("gpt-oss-20b", "excluded", evidence=SEED_EVIDENCE, category_rank=222, synced_at=stale)],
        stale,
    )
    routes = {
        f"https://llmstats.test/stats/v1/rankings?category=code&limit=50": _Response(200, _rankings_body([])),
        f"https://llmstats.test/stats/v1/rankings?category=general&limit=50": _Response(200, _rankings_body([])),
        f"https://llmstats.test/stats/v1/rankings?category=reasoning&limit=50": _Response(200, _rankings_body([])),
    }
    config = _config(tmp_path)
    summary = await sync_llm_stats_rankings(
        config,
        [("ollama-cloud", "gpt-oss:20b")],
        _FakeSession(routes),
    )
    assert summary["seed_kept"] == 0
    assert summary["unknown"] == 1
    row = db.rows()["gpt-oss-20b"]
    assert row["evidence"] == "not_in_category_window"
    assert row["status"] == "unknown"


@pytest.mark.asyncio
async def test_sync_window_evidence_overrides_seed_row(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    seed_at = time.time()
    db.replace_all(
        [_verdict("minimax-m3", "excluded", evidence=SEED_EVIDENCE, category_rank=400, synced_at=seed_at)],
        seed_at,
    )
    # Window now says pass — window evidence must always win.
    routes = {
        f"https://llmstats.test/stats/v1/rankings?category=code&limit=50": _Response(
            200, _rankings_body([("minimax-m3", 5)])
        ),
        f"https://llmstats.test/stats/v1/rankings?category=general&limit=50": _Response(200, _rankings_body([])),
        f"https://llmstats.test/stats/v1/rankings?category=reasoning&limit=50": _Response(200, _rankings_body([])),
    }
    config = _config(tmp_path)
    summary = await sync_llm_stats_rankings(
        config,
        [("minimax", "MiniMax-M3")],
        _FakeSession(routes),
    )
    assert summary["passed"] == 1
    row = db.rows()["minimax-m3"]
    assert row["evidence"] == "category_window"
    assert row["status"] == "pass"


@pytest.mark.asyncio
async def test_sync_zero_seed_ttl_disables_retention(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    seed_at = time.time()
    db.replace_all(
        [_verdict("gpt-oss-20b", "excluded", evidence=SEED_EVIDENCE, category_rank=222, synced_at=seed_at)],
        seed_at,
    )
    routes = {
        f"https://llmstats.test/stats/v1/rankings?category=code&limit=50": _Response(200, _rankings_body([])),
        f"https://llmstats.test/stats/v1/rankings?category=general&limit=50": _Response(200, _rankings_body([])),
        f"https://llmstats.test/stats/v1/rankings?category=reasoning&limit=50": _Response(200, _rankings_body([])),
    }
    config = _config(tmp_path, llm_stats_seed_max_age_secs=0.0)
    summary = await sync_llm_stats_rankings(
        config,
        [("ollama-cloud", "gpt-oss:20b")],
        _FakeSession(routes),
    )
    assert summary["seed_kept"] == 0
    assert summary["unknown"] == 1


# ---------------------------------------------------------------------------
# Tool: build_seed_rows + dry-run end-to-end
# ---------------------------------------------------------------------------


def test_build_seed_rows_seeds_only_out_of_window_models():
    windows = {
        "code": {"in-window-a": 1, "in-window-b": 2},
        "general": {"in-window-a": 5},
    }
    arrays = [
        (
            "code",
            [("in-window-a", 1), ("in-window-b", 2), ("deep-c", 100), ("deep-d", 200)],
        ),
        (
            "general",
            [("in-window-a", 5), ("deep-e", 50)],
        ),
    ]
    pairs = [
        ("p1", "in-window-a"),  # must be skipped (in window)
        ("p2", "deep-c"),       # best rank 100 → excluded
        ("p3", "deep-e"),       # best rank 50  → pass (≤25? no, 50>25 → excluded)
    ]
    rows, matches = build_seed_rows(
        windows, arrays, pairs, max_rank=25, synced_at=1000.0
    )
    assert sorted(matches) == ["code", "general"]
    assert sorted(r["slug"] for r in rows) == ["deep-c", "deep-e"]
    # All rows are 'excluded' since neither rank ≤ 25; in-window model skipped.
    assert all(r["status"] == "excluded" for r in rows)
    assert all(r["evidence"] == "site_seed" for r in rows)
    assert all(r["synced_at"] == 1000.0 for r in rows)
    # Best rank wins across matched categories.
    assert {r["slug"]: r["category_rank"] for r in rows} == {
        "deep-c": 100,
        "deep-e": 50,
    }


def test_build_seed_rows_passes_model_within_cutoff():
    windows = {"code": {"alpha": 1}}
    arrays = [("code", [("alpha", 1), ("beta", 12)])]
    pairs = [("p1", "beta")]
    rows, _ = build_seed_rows(windows, arrays, pairs, max_rank=25, synced_at=0.0)
    assert rows == [
        {
            "slug": "beta",
            "status": "pass",
            "category_rank": 12,
            "category_name": "code",
            "evidence": "site_seed",
            "synced_at": 0.0,
        }
    ]


@pytest.mark.asyncio
async def test_seed_tool_dry_run_does_not_write(tmp_path, monkeypatch):
    from tusker_gateway.tools import import_llm_stats_seed as tool

    html = _escaped_payload(
        '{"initialAllIndexes":{'
        '"code":{"category_id":"code","models":['
        '{"model_id":"alpha","rank":1,"name":"A"},'
        '{"model_id":"beta","rank":12,"name":"B"}'
        ']}}}',
    )
    html_path = tmp_path / "home.html"
    html_path.write_text(html)

    async def fake_fetch(config, session, categories):  # noqa: ARG001
        return ({"code": {"alpha": 1}}, 1, False)

    monkeypatch.setattr(tool, "fetch_category_windows", fake_fetch)

    db_path = tmp_path / "llm_stats.db"
    monkeypatch.setattr(
        "tusker_gateway.tools.import_llm_stats_seed.load_config",
        lambda: {
            "llm_stats_db_path": str(db_path),
            "llm_stats_api_key": "test-key",
            "llm_stats_base_url": "https://llmstats.test/stats/v1",
            "llm_stats_categories": ("code",),
            "llm_stats_max_rank": 25,
            "llm_stats_pacing_secs": 0,
            "llm_stats_seed_max_age_secs": 7 * 86_400,
            "llm_stats_site_url": "https://llm-stats.com/",
            "pools": {
                "code": PoolConfig(
                    name="code",
                    models=[{"provider": "p1", "model": "beta"}],
                )
            },
        },
    )

    class _Args:
        html = str(html_path)
        dry_run = True
        json = True
        log_level = "WARNING"

    rc = await tool._run(_Args())
    assert rc == 0
    assert db_path.exists() is False or ModelRankingsDB(str(db_path)).rows() == {}


# ---------------------------------------------------------------------------
# Strength probe (apply_probe / expire_probe / grading / calibration)
# ---------------------------------------------------------------------------


def test_apply_probe_skips_window_and_seed_but_overwrites_probe(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [
            _verdict("minimax-m3", "pass", category_rank=46, evidence="category_window"),
            _verdict("big-pickle", "pass", category_rank=60, category_name="probe"),
        ],
        time.time(),
    )
    outcome = db.apply_probe(
        [
            {
                "slug": "minimax-m3",
                "status": "pass",
                "category_rank": 3,
                "category_name": "probe",
            },
            {
                "slug": "big-pickle",
                "status": "pass",
                "category_rank": 12,
                "category_name": "probe",
            },
            {
                "slug": "syn-small-text",
                "status": "pass",
                "category_rank": 80,
                "category_name": "probe",
            },
        ],
        time.time(),
    )

    assert outcome == {"written": 2, "skipped_evidence": 1}
    rows = db.rows()
    assert rows["minimax-m3"]["category_rank"] == 46  # window evidence wins
    assert rows["big-pickle"]["category_rank"] == 12  # refreshed
    assert rows["syn-small-text"]["evidence"] == "probe"


def test_apply_probe_preserves_site_seed(tmp_path):
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [_verdict("gpt-oss-20b", "excluded", category_rank=191, evidence="site_seed")],
        time.time(),
    )
    outcome = db.apply_probe(
        [
            {
                "slug": "gpt-oss-20b",
                "status": "pass",
                "category_rank": 5,
                "category_name": "probe",
            }
        ],
        time.time(),
    )

    assert outcome["written"] == 0
    assert db.rows()["gpt-oss-20b"]["evidence"] == "site_seed"


def test_expire_probe_removes_only_stale_probe_rows(tmp_path):
    now = time.time()
    db = ModelRankingsDB(str(tmp_path / "llm_stats.db"))
    db.replace_all(
        [
            _verdict("old-probe", "pass", category_rank=40, evidence="probe", synced_at=now - 20 * 86_400),
            _verdict("fresh-probe", "pass", category_rank=41, evidence="probe", synced_at=now - 60.0),
            _verdict("old-window", "pass", category_rank=42, evidence="category_window", synced_at=now - 90 * 86_400),
        ],
        now,
    )

    removed = db.expire_probe(14 * 86_400.0, now)

    assert removed == 1
    rows = db.rows()
    assert "old-probe" not in rows
    assert "fresh-probe" in rows
    assert "old-window" in rows


def _chat_payload(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _tools_payload(name: str | None, arguments: Any) -> dict[str, Any]:
    calls = (
        []
        if name is None
        else [{"function": {"name": name, "arguments": arguments}}]
    )
    return {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}


def test_probe_grades_coding_and_reasoning_items():
    from tusker_gateway.tools.probe_strength import QUESTIONS, grade

    by_note = {q["grade_note"]: q for q in QUESTIONS if q["mode"] == "chat"}
    slice_q = by_note["slice excludes index 3"]
    rec_q = by_note["120 - 24 = 96"]
    assert grade(_chat_payload("yak ANSWER: [2, 3]"), slice_q)
    assert not grade(_chat_payload("ANSWER: [1, 2, 3, 4]"), slice_q)
    assert grade(_chat_payload("ANSWER: 96"), rec_q)
    assert not grade(_chat_payload("ANSWER: 120"), rec_q)


def test_probe_grades_tool_calls_by_name_and_arguments():
    from tusker_gateway.tools.probe_strength import QUESTIONS, grade

    tool_qs = {q["expect_name"]: q for q in QUESTIONS if q["mode"] == "tools"}
    read_q = tool_qs["read_file"]
    ticket_q = tool_qs["create_ticket"]
    inhibit_q = tool_qs[None]

    assert grade(
        _tools_payload("read_file", '{"path": "/etc/hostname"}'), read_q
    )
    assert not grade(
        _tools_payload("read_file", '{"path": "/etc/passwd"}'), read_q
    )
    assert not grade(_tools_payload("write_file", '{"path": "/etc/hostname"}'), read_q)
    # Two calls for a single-call instruction is a compliance failure.
    twice = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "read_file", "arguments": '{"path": "/etc/hostname"}'}},
        {"function": {"name": "read_file", "arguments": '{"path": "/etc/hostname"}'}},
    ]}}]}
    assert not grade(twice, read_q)
    # Broken JSON arguments fail instead of crashing the grader.
    assert not grade(_tools_payload("create_ticket", "{not json"), ticket_q)
    # Multi-arg fidelity: array + enum must match exactly.
    assert grade(
        _tools_payload(
            "create_ticket",
            '{"title": "DB latency", "priority": "high", '
            '"assignee": "ada", "labels": ["ops", "urgent"]}',
        ),
        ticket_q,
    )
    assert not grade(
        _tools_payload(
            "create_ticket",
            '{"title": "DB latency", "priority": "high", '
            '"assignee": "ada", "labels": ["urgent", "ops"]}',
        ),
        ticket_q,
    )
    # Inhibition item: any tool call is a failure, none is correct.
    assert grade(_tools_payload(None, "{}"), inhibit_q)
    assert not grade(
        _tools_payload("get_weather", '{"city": "Paris"}'), inhibit_q
    )


def test_probe_interpolation_matches_reference_ranks():
    from tusker_gateway.tools.probe_strength import _interpolate_rank

    curve = [(100.0, 3), (70.0, 15), (20.0, 191)]
    # Reference scores resolve to their own ranks (via the piecewise map).
    assert _interpolate_rank(100.0, curve) == 3
    assert _interpolate_rank(70.0, curve) == 15
    assert _interpolate_rank(20.0, curve) == 191
    # Between references: monotone, rank-decreasing with score.
    mid = _interpolate_rank(85.0, curve)
    assert 3 < mid < 15
    assert _interpolate_rank(90.0, curve) < mid < _interpolate_rank(75.0, curve)
    # Beyond the curve: extrapolate one step, clamped to the ladder.
    assert _interpolate_rank(105.0, curve) == 2
    assert _interpolate_rank(0.0, curve) == 192
    assert _interpolate_rank(50.0, []) is None


def test_probe_curve_conflict_detects_inverted_references():
    from tusker_gateway.tools.probe_strength import _calibrate, _curve_conflict

    # A rank-191 model outscoring a rank-15 model invalidates the curve.
    curve = _calibrate(
        [{"score": 100.0, "rank": 191}, {"score": 92.3, "rank": 15}]
    )
    assert _curve_conflict(curve)

    # Consistent references keep the curve usable.
    good = _calibrate(
        [{"score": 100.0, "rank": 3}, {"score": 70.0, "rank": 15}]
    )
    assert not _curve_conflict(good)
