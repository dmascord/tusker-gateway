"""Tests for value-weighted route scoring: plan economics, pacing, privacy.

The value router scores candidates on "intel per dollar under the provider's
subscription plan" instead of quality alone. These tests pin the contracts
that keep that safe:

* a plan multiplier discounts the blended cost, never the quality score;
* an unpriced route can never win on unsubstantiated value;
* a subscription window decays the value term and withholds the route once
  the window is exhausted — the quality score is untouched;
* a provider that trains on request data is hard-masked from ZDR pools;
* shadow mode records what value routing *would* have picked without
  changing the selection.
"""
from __future__ import annotations

import time

import pytest

from tusker_gateway.config import PoolConfig
from tusker_gateway.pools import PoolManager
from tusker_gateway.provider_usage import ProviderUsageDB
from tusker_gateway.quality import QualityDB
from tusker_gateway.value import (
    BudgetPacer,
    IntelPrior,
    ProviderPlan,
    RouteCostModel,
    UsageMix,
    load_provider_plans,
    record_shadow_comparison,
    reset_shadow_state_for_tests,
    score_candidates,
    shadow_comparison_snapshot,
    value_routing_mode,
)


class _FakeEntry:
    def __init__(self, model: str, cost_input: float | None, cost_output: float | None):
        self.model = model
        self.cost_input = cost_input
        self.cost_output = cost_output


class _FakeRegistry:
    """Minimal stand-in for the catalog registry."""

    def __init__(self, entries: dict[str, list[_FakeEntry]]):
        self._entries = entries

    def providers(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def entries_for(self, provider: str) -> list[_FakeEntry] | None:
        return self._entries.get(provider)


class _FakeUsageDB:
    """Usage ledger stub returning pre-seeded daily rows."""

    def __init__(self, rows: list[tuple[str, str, str, int, int]]):
        self._rows = rows

    def window_spend_rows(self) -> list[tuple[str, str, str, int, int]]:
        return list(self._rows)


# ---------------------------------------------------------------------------
# Plan configuration
# ---------------------------------------------------------------------------


def test_plan_multiplier_and_windows_parse_from_config():
    plans = load_provider_plans(
        {
            "provider_plans": {
                "CommandCode": {
                    "multiplier": 0.143,
                    "window_5h_usd": 14.0,
                    "window_week_usd": 35.0,
                },
                "openai_codex": {"multiplier": 1.0},
            }
        }
    )
    assert set(plans) == {"commandcode", "openai-codex"}
    plan = plans["commandcode"]
    assert plan.multiplier == pytest.approx(0.143)
    assert plan.window_5h_usd == 14.0
    assert plan.window_week_usd == 35.0
    assert plans["openai-codex"].multiplier == 1.0


def test_invalid_plan_entries_are_skipped_not_fatal():
    plans = load_provider_plans(
        {
            "provider_plans": {
                "good": {"multiplier": 0.5},
                "bad_multiplier": {"multiplier": 4.0},
                "bad_shape": "nope",
            }
        }
    )
    assert set(plans) == {"good"}


def test_provider_plans_must_be_an_object():
    assert load_provider_plans({"provider_plans": ["a", "b"]}) == {}


# ---------------------------------------------------------------------------
# Intel prior
# ---------------------------------------------------------------------------


def test_intel_prior_maps_rank_onto_band():
    prior = IntelPrior()
    assert prior.prior(1) == 100.0
    assert prior.prior(None) == 50.0
    assert prior.prior(0) == 50.0
    assert prior.prior(41) == 0.0
    assert 0.0 < prior.prior(15) < 100.0


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------


def test_blended_cost_applies_plan_multiplier():
    registry = _FakeRegistry(
        {"p": [_FakeEntry("m", 10.0, 30.0)]}
    )
    model = RouteCostModel(registry)
    cost = model.blended_cost(
        "p", "m", multiplier=0.1, input_fraction=1.0
    )
    assert cost == pytest.approx(1.0)


def test_unknown_pricing_uses_demotion_fallback_cost():
    model = RouteCostModel(_FakeRegistry({"p": []}))
    cost = model.blended_cost(
        "p", "m", multiplier=1.0, input_fraction=0.75
    )
    assert cost == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score(**kwargs):
    defaults = {
        "pool_name": "code",
        "candidates": [("a", "m1"), ("b", "m2")],
        "quality_scores": {("a", "m1"): 90.0, ("b", "m2"): 70.0},
        "latency_ms": {},
        "plans": {},
        "intel": IntelPrior(),
        "ranks_by_slug": {},
        "cost_model": RouteCostModel(_FakeRegistry({})),
        "input_fraction": 0.75,
        "pacer": None,
    }
    defaults.update(kwargs)
    return score_candidates(**defaults)


def test_cheaper_route_wins_when_quality_is_equal():
    registry = _FakeRegistry(
        {
            "cheap": [_FakeEntry("m1", 0.1, 0.2)],
            "dear": [_FakeEntry("m2", 10.0, 30.0)],
        }
    )
    scores = _score(
        candidates=[("cheap", "m1"), ("dear", "m2")],
        quality_scores={("cheap", "m1"): 80.0, ("dear", "m2"): 80.0},
        cost_model=RouteCostModel(registry),
    )
    best = max(scores, key=lambda s: s.final)
    assert (best.provider, best.model) == ("cheap", "m1")


def test_plan_discount_makes_a_expensive_route_competitive():
    registry = _FakeRegistry(
        {
            "sub": [_FakeEntry("m1", 10.0, 30.0)],
            "payg": [_FakeEntry("m2", 10.0, 30.0)],
        }
    )
    scores = _score(
        candidates=[("sub", "m1"), ("payg", "m2")],
        quality_scores={("sub", "m1"): 80.0, ("payg", "m2"): 80.0},
        plans={"sub": ProviderPlan(provider="sub", multiplier=0.1)},
        cost_model=RouteCostModel(registry),
    )
    best = max(scores, key=lambda s: s.final)
    assert best.provider == "sub"


def test_quality_dominates_value_so_a_flaky_cheap_route_does_not_win():
    registry = _FakeRegistry(
        {
            "cheap": [_FakeEntry("m1", 0.01, 0.02)],
            "dear": [_FakeEntry("m2", 100.0, 300.0)],
        }
    )
    scores = _score(
        candidates=[("cheap", "m1"), ("dear", "m2")],
        quality_scores={("cheap", "m1"): 20.0, ("dear", "m2"): 95.0},
        cost_model=RouteCostModel(registry),
    )
    best = max(scores, key=lambda s: s.final)
    assert best.provider == "dear"


def test_unpriced_route_never_outranks_a_priced_one():
    registry = _FakeRegistry(
        {"priced": [_FakeEntry("m2", 1.0, 2.0)]}
    )
    scores = _score(
        candidates=[("unpriced", "m1"), ("priced", "m2")],
        quality_scores={("unpriced", "m1"): 80.0, ("priced", "m2"): 80.0},
        cost_model=RouteCostModel(registry),
    )
    best = max(scores, key=lambda s: s.final)
    assert best.provider == "priced"


# ---------------------------------------------------------------------------
# Budget pacing
# ---------------------------------------------------------------------------


def test_pacing_leaves_value_untouched_below_the_threshold():
    registry = _FakeRegistry({"p": [_FakeEntry("m", 10.0, 10.0)]})
    cost_model = RouteCostModel(registry)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    # 7 USD of list-price spend against a 10 USD window: 70% < 80%.
    rows = [("p", "m", today, 525_000, 175_000)]  # 7 USD at $10/M
    pacer = BudgetPacer(_FakeUsageDB(rows), cost_model, threshold=0.8)
    plan = ProviderPlan(provider="p", multiplier=1.0, window_5h_usd=10.0)
    assert pacer.decay(plan) == 1.0
    assert pacer.windowed_out(plan) is False


def test_pacing_decays_value_above_the_threshold():
    registry = _FakeRegistry({"p": [_FakeEntry("m", 10.0, 10.0)]})
    cost_model = RouteCostModel(registry)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    # 9 USD against a 10 USD window: 90% > 80% → decay, but not exhausted.
    rows = [("p", "m", today, 675_000, 225_000)]  # 9 USD at $10/M
    pacer = BudgetPacer(_FakeUsageDB(rows), cost_model, threshold=0.8)
    plan = ProviderPlan(provider="p", multiplier=1.0, window_5h_usd=10.0)
    decay = pacer.decay(plan)
    assert 0.0 < decay < 1.0
    assert pacer.windowed_out(plan) is False


def test_pacing_withholds_the_route_once_the_window_is_exhausted():
    registry = _FakeRegistry({"p": [_FakeEntry("m", 10.0, 10.0)]})
    cost_model = RouteCostModel(registry)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    rows = [("p", "m", today, 1_000_000, 500_000)]
    pacer = BudgetPacer(_FakeUsageDB(rows), cost_model, threshold=0.8)
    plan = ProviderPlan(provider="p", multiplier=1.0, window_5h_usd=10.0)
    assert pacer.decay(plan) == 0.0
    assert pacer.windowed_out(plan) is True


def test_pacing_ignores_spend_older_than_the_week_window():
    registry = _FakeRegistry({"p": [_FakeEntry("m", 10.0, 10.0)]})
    cost_model = RouteCostModel(registry)
    old_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
    rows = [("p", "m", old_day, 10_000_000, 5_000_000)]
    pacer = BudgetPacer(_FakeUsageDB(rows), cost_model, threshold=0.8)
    plan = ProviderPlan(provider="p", multiplier=1.0, window_week_usd=10.0)
    assert pacer.decay(plan) == 1.0


def test_pacing_without_windows_never_decays():
    pacer = BudgetPacer(None, RouteCostModel(None))
    plan = ProviderPlan(provider="p", multiplier=1.0)
    assert pacer.decay(plan) == 1.0
    assert pacer.windowed_out(plan) is False


def test_exhausted_window_decays_the_score_not_the_quality():
    registry = _FakeRegistry({"p": [_FakeEntry("m", 10.0, 10.0)]})
    cost_model = RouteCostModel(registry)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    rows = [("p", "m", today, 1_000_000, 500_000)]
    pacer = BudgetPacer(_FakeUsageDB(rows), cost_model, threshold=0.8)
    plan = ProviderPlan(provider="p", multiplier=1.0, window_5h_usd=10.0)
    scores = _score(
        candidates=[("p", "m"), ("q", "m2")],
        quality_scores={("p", "m"): 90.0, ("q", "m2"): 60.0},
        plans={"p": plan},
        cost_model=cost_model,
        pacer=pacer,
    )
    spent = next(s for s in scores if s.provider == "p")
    assert spent.pacing_factor == 0.0
    assert spent.quality == 90.0


# ---------------------------------------------------------------------------
# Usage mix
# ---------------------------------------------------------------------------


def test_usage_mix_falls_back_to_the_default_ratio_without_data():
    mix = UsageMix(None)
    assert mix.input_fraction("code") == pytest.approx(0.75)


def test_usage_mix_reads_observed_token_split():
    db = ProviderUsageDB(":memory:")
    db.record(
        provider="p",
        model="m",
        success=True,
        prompt_tokens=300,
        completion_tokens=100,
    )
    mix = UsageMix(db)
    assert mix.input_fraction("code") == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------


def test_value_routing_mode_defaults_to_shadow(monkeypatch):
    monkeypatch.delenv("TUSKER_VALUE_ROUTING", raising=False)
    assert value_routing_mode() == "shadow"


def test_unknown_value_routing_mode_falls_back_to_shadow(monkeypatch):
    monkeypatch.setenv("TUSKER_VALUE_ROUTING", "nonsense")
    assert value_routing_mode() == "shadow"


def test_value_routing_mode_reads_the_environment_per_call(monkeypatch):
    monkeypatch.setenv("TUSKER_VALUE_ROUTING", "actual")
    assert value_routing_mode() == "actual"
    monkeypatch.setenv("TUSKER_VALUE_ROUTING", "off")
    assert value_routing_mode() == "off"


# ---------------------------------------------------------------------------
# Shadow bookkeeping
# ---------------------------------------------------------------------------


def test_shadow_comparison_records_the_divergence():
    reset_shadow_state_for_tests()
    record_shadow_comparison("code", ("a", "m1"), ("b", "m2"))
    snapshot = shadow_comparison_snapshot()
    assert snapshot["code"] == [("a/m1->b/m2", 1)]
    reset_shadow_state_for_tests()
    assert shadow_comparison_snapshot() == {}


# ---------------------------------------------------------------------------
# Pool integration: privacy mask and selection wiring
# ---------------------------------------------------------------------------


def _manager(tmp_path, *, plans: dict | None = None, zdr: bool = False) -> PoolManager:
    config = {
        "quality_db_path": str(tmp_path / "quality.db"),
        "providers": {
            "alpha": {"base_url": "http://alpha.invalid", "zdr_ok": True},
            "beta": {"base_url": "http://beta.invalid", "zdr_ok": True},
        },
        "pools": {
            "code": PoolConfig(
                "code",
                [
                    {"provider": "alpha", "model": "m1"},
                    {"provider": "beta", "model": "m2"},
                ],
            ),
            "privacy": PoolConfig(
                "privacy",
                [
                    {"provider": "alpha", "model": "m1"},
                    {"provider": "beta", "model": "m2"},
                ],
                zdr=zdr,
            ),
        },
        "provider_plans": plans or {},
    }
    return PoolManager(config)


def test_trains_on_data_provider_is_masked_from_zdr_pools(tmp_path):
    manager = _manager(
        tmp_path,
        plans={"alpha": {"trains_on_data": True}},
        zdr=True,
    )
    picked = manager.select("privacy")
    assert picked is not None
    assert picked[0] == "beta"


def test_trains_on_data_provider_stays_available_in_non_private_pools(tmp_path):
    manager = _manager(
        tmp_path,
        plans={"alpha": {"trains_on_data": True}},
        zdr=False,
    )
    selected = {
        manager.select("code")[0] for _ in range(40)
    }
    assert "alpha" in selected


def test_privacy_context_limit_ignores_larger_non_zdr_routes(tmp_path, monkeypatch):
    """A privacy overflow must describe only routes privacy traffic may use."""
    from tusker_gateway.context_limits import required_context_tokens

    monkeypatch.setenv(
        "TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON",
        '{"alpha/m1": 1000000, "beta/m2": 32768}',
    )
    manager = _manager(
        tmp_path,
        plans={"alpha": {"trains_on_data": True}},
        zdr=True,
    )
    context_failure: dict[str, int] = {}

    assert manager.select(
        "privacy",
        context_tokens=required_context_tokens(40000),
        context_failure=context_failure,
    ) is None
    assert context_failure == {
        "required_context_tokens": required_context_tokens(40000),
        "max_context_window": 32768,
    }



def test_shadow_mode_leaves_selection_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_VALUE_ROUTING", "shadow")
    manager = _manager(
        tmp_path,
        plans={"alpha": {"multiplier": 0.01}, "beta": {"multiplier": 1.0}},
    )
    quality = QualityDB(str(tmp_path / "quality.db"))
    quality.record("alpha", "m1", True, 100.0)
    quality.record("beta", "m2", True, 100.0)
    manager._quality = quality
    picked = manager.select("code")
    assert picked is not None
    # Quality is equal, so shadow mode must not change the outcome class.
    assert picked[0] in {"alpha", "beta"}


def test_actual_mode_prefers_the_cheaper_plan_when_quality_is_equal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TUSKER_VALUE_ROUTING", "actual")
    manager = _manager(
        tmp_path,
        plans={"alpha": {"multiplier": 0.01}, "beta": {"multiplier": 1.0}},
    )
    quality = QualityDB(str(tmp_path / "quality.db"))
    quality.record("alpha", "m1", True, 100.0)
    quality.record("beta", "m2", True, 100.0)
    manager._quality = quality
    picked = manager.select("code")
    assert picked is not None
    assert picked[0] == "alpha"


def test_off_mode_skips_value_scoring(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_VALUE_ROUTING", "off")
    manager = _manager(
        tmp_path,
        plans={"alpha": {"multiplier": 0.01}},
    )
    assert manager.select("code") is not None
