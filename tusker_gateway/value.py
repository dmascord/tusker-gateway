"""Value-weighted route scoring: intel-per-dollar under subscription plans.

The quality score answers "how reliably does this route serve requests"; the
LLM-Stats rank boost answers "how strong is the model". Neither asks the
operator's real question: is the model strength worth what the route costs
under the subscription it bills against? This module adds that dimension as a
third score term, plus hard eligibility masks that must never be softened by
any score.

Formula (per candidate, computed at selection time):

    blended_cost = (in_rate * in_frac + out_rate * out_frac) * plan_multiplier
    value        = intel_prior / blended_cost            (intel per USD)
    value_norm   = 0-100 within the candidate set (log-scaled)
    final        = q_weight * quality
                 + v_weight * value_norm
                 + s_weight * speed_norm

* ``intel_prior`` comes from the LLM Stats rank snapshot inverted into a
  0-100 band: rank 1 → 100, rank ``max`` → 0, unranked → the pool default
  (50). Ranks move within a day and coverage is partial, so the prior is
  deliberately soft: a stronger prior cannot rescue a route with bad live
  quality because quality keeps the largest weight by default.
* ``in_frac`` / ``out_frac`` are the observed prompt/completion token mix
  from the provider usage DB for the pool; absent usage data falls back to
  the cost-estimate default (3:1 input:output), and unknown rates fall back
  to blended-cost 1.0 so an unpriced route scores value 0 rather than
  winning by accident (mirrors the unpriced-entry demotion rule).
* ``plan_multiplier`` is the subscription discount (e.g. $10 plan giving $70
  of credits → 0.143). Routes billed PAYG use 1.0.
* A subscription window is a hard budget, not a discount on quality, so
  windowed spend applies a *decay* to the value term (never to quality):
  below ``pacing_threshold`` the multiplier is untouched; above it the
  effective multiplier rises linearly until the window cap where the route
  is fully withheld for the rest of the window. This reproduces the
  "CC GOAT fresh → OC Go throttled → CC Pro" waterfall automatically.

Modes (``TUSKER_VALUE_ROUTING``, per-request env read like the
normalization shadow flag):

* ``shadow`` (default): compute and log/aggregate the score for every
  candidate but selection behaves exactly as before. The comparison
  counters record what *would* have won.
* ``actual``: the value-adjusted score replaces the plain quality ranking
  in ``PoolManager.select``.
* ``off``: skip the scorer entirely (no logging, no cost).

Privacy is never a score term. ``trains_on_data`` routes are masked from
ZDR pools at candidate-filter time (selection.eligibility), the same way
``zdr_ok`` gates the privacy pool; non-private pools keep them eligible so
the cheap tier can still use a Contributor-class route for non-sensitive
work.
"""
from __future__ import annotations

import logging

import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from tusker_gateway.model_rankings import normalize_slug

logger = logging.getLogger(__name__)

# Default token mix when the usage DB has no observations for a pool yet.
# 3:1 input:output mirrors the payload shape of agentic coding traffic.
DEFAULT_INPUT_FRACTION = 0.75

# Synthetic intel for an unraked slug: dead center. The prior must not make
# an unknown model look either excellent or worthless.
UNRANKED_INTEL = 50.0

# LLM Stats category rank mapped onto this band: rank 1 → 100, rank
# ``INTEL_RANK_SPAN`` and beyond → 0.
INTEL_RANK_SPAN = 40.0

# Fallback blended cost (USD per 1M tokens, blended at the default mix)
# for routes whose pricing is unknown. Value = intel / cost with this
# fallback yields a low-but-nonzero score so unpriced routes rank below
# every priced route instead of dividing by zero.
UNKNOWN_COST_FALLBACK = 1.0

# --- Configuration -------------------------------------------------------

VALUE_ROUTING_MODES = ("shadow", "actual", "off")
DEFAULT_VALUE_ROUTING_MODE = "shadow"

# Score-term weights. Quality keeps the majority: the value term reallocates
# traffic among *reliable* routes; it must not promote a flaky cheap route.
DEFAULT_QUALITY_WEIGHT = 0.6
DEFAULT_VALUE_WEIGHT = 0.3
DEFAULT_SPEED_WEIGHT = 0.1

# Fraction of a subscription window at which pacing decay begins.
DEFAULT_BUDGET_PACING_THRESHOLD = 0.8

# Plan multipliers sourced from the operator's active subscriptions
# (docs/gateway-model-routing.md documents the schema). Keys are provider
# names; the values carry the plan economics and the privacy policy flags.
# Prices in this file are defaults ONLY; the catalog overrides them per
# route when the authoritative catalog publishes pricing.
DEFAULT_PROVIDER_PLANS: dict[str, dict[str, Any]] = {}


def value_routing_mode() -> str:
    """Resolve the per-request value-routing mode.

    Read per call (no startup caching) so an operator can flip the mode in
    the deployment manifest without a restart, mirroring
    ``normalization.shadow_mode_enabled``. Unknown values fall back to
    ``shadow``: the scorer stays observable but never changes behavior.
    """
    raw = os.environ.get("TUSKER_VALUE_ROUTING", DEFAULT_VALUE_ROUTING_MODE)
    mode = str(raw).strip().lower()
    return mode if mode in VALUE_ROUTING_MODES else DEFAULT_VALUE_ROUTING_MODE


def load_provider_plans(config: dict[str, Any]) -> dict[str, "ProviderPlan"]:
    """Parse ``provider_plans`` config into ProviderPlan objects.

    Config may arrive as JSON (env/DB config store) or as a dict in tests.
    Malformed entries log a warning and are skipped: a bad plan must not
    break selection.
    """
    raw = config.get("provider_plans") or {}
    plans: dict[str, ProviderPlan] = {}
    if not isinstance(raw, dict):
        logger.warning("provider_plans config must be an object; ignoring")
        return plans
    for provider, entry in raw.items():
        if not isinstance(entry, dict):
            logger.warning(
                "provider_plans['%s'] must be an object; ignoring", provider
            )
            continue
        try:
            plans[str(provider).strip().lower().replace("_", "-")] = (
                ProviderPlan.from_dict(provider, entry)
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "provider_plans['%s'] invalid (%s); ignoring", provider, exc
            )
    return plans


@dataclass(frozen=True)
class ProviderPlan:
    """Subscription economics and privacy policy for one provider."""

    provider: str
    # Effective cost multiplier: what $1 of list price actually bills.
    # PAYG providers use 1.0. A $10/mo plan granting $70 of credits → 0.143.
    multiplier: float = 1.0
    # Rolling subscription windows in USD of *list-price* spend, matching
    # how subscription plans meter usage. None = no window (unlimited).
    window_5h_usd: float | None = None
    window_week_usd: float | None = None
    # Provider shares request data for training. Hard-masked from ZDR
    # pools regardless of zdr_ok, and surfaced in /status.
    trains_on_data: bool = False
    # Retention override (days). 0 = zero-day retention. None = unknown.
    retention_days: int | None = None

    @classmethod
    def from_dict(cls, provider: str, entry: dict[str, Any]) -> "ProviderPlan":
        multiplier = float(entry.get("multiplier", 1.0))
        if not 0.0 < multiplier <= 1.0:
            raise ValueError(f"multiplier must be in (0, 1]: {multiplier}")

        def _window(key: str) -> float | None:
            value = entry.get(key)
            if value is None:
                return None
            parsed = float(value)
            if parsed < 0:
                raise ValueError(f"{key} must be >= 0: {parsed}")
            return parsed

        retention = entry.get("retention_days")
        return cls(
            provider=str(provider).strip().lower().replace("_", "-"),
            multiplier=multiplier,
            window_5h_usd=_window("window_5h_usd"),
            window_week_usd=_window("window_week_usd"),
            trains_on_data=bool(entry.get("trains_on_data", False)),
            retention_days=int(retention) if retention is not None else None,
        )


# --- Intel prior ---------------------------------------------------------

_RANK_CACHE_TTL_SECS = 5.0


class IntelPrior:
    """LLM-Stats category rank inverted into a soft 0-100 strength prior."""

    def __init__(self, span: float = INTEL_RANK_SPAN):
        self._span = span
        self._cache: dict[str, tuple[float, float]] = {}
        self._cached_at = 0.0
        self._lock = threading.Lock()

    def prior(self, rank: int | None) -> float:
        """Map one category rank onto the 0-100 intel band."""
        if rank is None or rank <= 0:
            return UNRANKED_INTEL
        return max(0.0, 100.0 - (rank - 1) * (100.0 / self._span))

    def prior_by_slug(self, slug: str, ranks: dict[str, int | None]) -> float:
        """Cached prior lookup for one normalized slug."""
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(slug)
            if cached is not None and now - cached[1] < _RANK_CACHE_TTL_SECS:
                return cached[0]
            prior = self.prior(ranks.get(slug))
            self._cache[slug] = (prior, now)
            return prior


# --- Cost model ----------------------------------------------------------

_COST_CACHE_TTL_SECS = 60.0


class RouteCostModel:
    """Blend list-price rates into one USD-per-1M cost per route.

    Rates come from the catalog registry (which merges models.dev and the
    provider-specific overrides). Unknown pricing yields the fallback cost,
    so an unpriced route can never win on value it cannot substantiate.
    """

    def __init__(self, catalog_registry: Any | None = None):
        self._registry = catalog_registry
        self._rates: dict[tuple[str, str], tuple[float | None, float | None]] = {}
        self._cached_at = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        now = time.monotonic()
        if self._rates and now - self._cached_at < _COST_CACHE_TTL_SECS:
            return
        rates: dict[tuple[str, str], tuple[float | None, float | None]] = {}
        entries_for = getattr(self._registry, "entries_for", None) if self._registry else None
        if callable(entries_for):
            providers_attr = getattr(self._registry, "providers", None)
            if callable(providers_attr):
                provider_names = list(providers_attr())
            elif isinstance(providers_attr, dict):
                provider_names = list(providers_attr)
                provider_names = list(providers_attr)
            for provider in provider_names:
                try:
                    entries = entries_for(provider) or []
                except Exception:
                    continue
                for entry in entries:
                    rates[(provider, entry.model)] = (
                        entry.cost_input,
                        entry.cost_output,
                    )
        with self._lock:
            self._rates = rates
            self._cached_at = now

    def rates(self, provider: str, model: str) -> tuple[float | None, float | None]:
        """Return (cost_input, cost_output) per 1M tokens, or (None, None)."""
        self._refresh()
        with self._lock:
            return self._rates.get((provider, model), (None, None))
    def has_pricing(self, provider: str, model: str) -> bool:
        """Whether the catalog publishes both rates for this route."""
        cost_in, cost_out = self.rates(provider, model)
        return cost_in is not None and cost_out is not None

    def blended_cost(
        self,
        provider: str,
        model: str,
        *,
        multiplier: float,
        input_fraction: float,
    ) -> float:
        """Return effective USD per 1M tokens after the plan discount."""
        cost_in, cost_out = self.rates(provider, model)
        if cost_in is None or cost_out is None:
            # Unknown pricing: the demotion default. Value computed against
            # this is worse than any fully-priced route.
            return UNKNOWN_COST_FALLBACK * multiplier
        return (cost_in * input_fraction + cost_out * (1.0 - input_fraction)) * multiplier


# --- Usage mix -----------------------------------------------------------

_MIX_CACHE_TTL_SECS = 60.0


class UsageMix:
    """Observed prompt/completion token split per pool, short-cached."""

    def __init__(self, usage_db: Any | None):
        self._db = usage_db
        self._cached: tuple[float, float] | None = None
        self._cached_at = 0.0
        self._lock = threading.Lock()

    def input_fraction(self, pool_name: str) -> float:
        """Return the pool's observed input-token fraction (0..1)."""
        if self._db is None:
            return DEFAULT_INPUT_FRACTION
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now - self._cached_at < _MIX_CACHE_TTL_SECS:
                if self._cached[1] > 0:
                    return self._cached[0] / self._cached[1]
                return DEFAULT_INPUT_FRACTION
        try:
            status = self._db.status()
        except Exception:
            return DEFAULT_INPUT_FRACTION
        prompt = 0
        completion = 0
        for group in (status or {}).get("groups", {}).values():
            prompt += int(group.get("prompt_tokens", 0))
            completion += int(group.get("completion_tokens", 0))
        with self._lock:
            self._cached = (float(prompt), float(completion))
            self._cached_at = now
        if completion + prompt == 0:
            return DEFAULT_INPUT_FRACTION
        return prompt / (prompt + completion)


# --- Window pacing -------------------------------------------------------

# Regex matching usage-day strings ("YYYY-MM-DD") for week-window math.
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_usage_day(day: str) -> float | None:
    """Return a UTC timestamp for a usage_day string, or None."""
    match = _DAY_RE.match(str(day or ""))
    if not match:
        return None
    year, month, day_num = (int(part) for part in match.groups())
    try:
        import calendar

        return calendar.timegm((year, month, day_num, 0, 0, 0))
    except (ValueError, OverflowError):
        return None


class BudgetPacer:
    """Track subscription-window spend per provider and derive pacing decay.

    Windows mirror subscription plans: a 5-hour rolling window and a weekly
    rolling window, both in list-price USD. Spend is derived from the
    provider usage DB's daily token counters at the observed token mix and
    catalog list prices (the multiplier is *not* applied to metered spend —
    subscription windows meter at list price, which is exactly what the
    plan's sub-limits measure).

    ``decay(provider, now)`` returns the effective multiplier factor:
    1.0 below the pacing threshold, linearly down to 0.0 at 100% of the
    tightest window, then the route is withheld entirely by the selector.
    """

    def __init__(
        self,
        usage_db: Any | None,
        cost_model: RouteCostModel,
        *,
        threshold: float = DEFAULT_BUDGET_PACING_THRESHOLD,
        input_fraction: float = DEFAULT_INPUT_FRACTION,
    ):
        self._db = usage_db
        self._cost_model = cost_model
        self._threshold = min(max(threshold, 0.1), 1.0)
        self._input_fraction = input_fraction
        self._cache: dict[str, dict[str, float]] = {}
        self._cached_at = 0.0
        self._lock = threading.Lock()

    def _spend_by_provider(self) -> dict[str, float]:
        """List-price USD spend per provider over the open windows."""
        now = time.monotonic()
        with self._lock:
            if self._cache and now - self._cached_at < _MIX_CACHE_TTL_SECS:
                return self._cache
        if self._db is None:
            return {}
        try:
            rows = self._db.window_spend_rows()
        except Exception:
            return {}
        # rows: [(provider, model, usage_day, prompt_tokens, completion_tokens)]
        week_ago = time.time() - 7 * 86400
        five_hours_ago = time.time() - 5 * 3600
        spend: dict[str, float] = {}
        for provider, model, day, prompt, completion in rows:
            ts = _parse_usage_day(day)
            if ts is None:
                continue
            if ts < week_ago:
                continue
            cost_in, cost_out = self._cost_model.rates(provider, model)
            if cost_in is None or cost_out is None:
                continue
            cost = (
                cost_in * self._input_fraction
                + cost_out * (1.0 - self._input_fraction)
            )
            tokens = prompt + completion
            usd = cost * tokens / 1_000_000
            current = spend.setdefault(provider, 0.0)
            # Weekly window: everything in range counts.
            spend[provider] = current + usd
            # 5-hour window only counts today's rows within 5h; daily
            # granularity means the whole of today counts when today
            # started within 5h of now, else the row is stale for 5h math.
            # Conservative: count today fully when today is the current day.
            if ts >= five_hours_ago or time.gmtime().tm_yday == time.gmtime(ts).tm_yday:
                spend.setdefault("__5h__" + provider, 0.0)
                spend["__5h__" + provider] += usd
        with self._lock:
            self._cache = spend
            self._cached_at = now
        return spend

    def decay(self, plan: ProviderPlan, now: float | None = None) -> float:
        """Return the pacing factor for one provider (1.0 = no decay)."""
        if plan.window_5h_usd is None and plan.window_week_usd is None:
            return 1.0
        spend = self._spend_by_provider()
        now = now if now is not None else time.time()
        factor = 1.0
        for limit_key, window_spend in (
            ("window_week_usd", spend.get(plan.provider, 0.0)),
            ("window_5h_usd", spend.get("__5h__" + plan.provider, 0.0)),
        ):
            limit = getattr(plan, limit_key)
            if limit is None or limit <= 0:
                continue
            ratio = window_spend / limit
            if ratio >= 1.0:
                return 0.0
            if ratio > self._threshold:
                # Linear decay from the threshold to the cap.
                span = 1.0 - self._threshold
                factor = min(factor, max(0.0, (1.0 - ratio) / span))
        return factor

    def windowed_out(self, plan: ProviderPlan) -> bool:
        """True when every configured window is exhausted for the provider."""
        if plan.window_5h_usd is None and plan.window_week_usd is None:
            return False
        return self.decay(plan) <= 0.0


class Counter:
    """Tiny multiset for shadow comparison counters (no collections dep)."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}

    def __getitem__(self, key: tuple[str, str]) -> int:
        return self._counts.get(key, 0)

    def add(self, key: tuple[str, str], amount: int = 1) -> None:
        self._counts[key] = self._counts.get(key, 0) + amount

    def items(self) -> list[tuple[tuple[str, str], int]]:
        return sorted(self._counts.items())

_SHADOW_LOCK = threading.Lock()
_SHADOW = Counter()


def record_shadow_comparison(
    pool_name: str,
    actual: tuple[str, str],
    value_pick: tuple[str, str],
) -> None:
    """Aggregate shadow-mode agreement between the two rankings."""
    key = (
        f"{actual[0]}/{actual[1]}",
        f"{value_pick[0]}/{value_pick[1]}",
    )
    _SHADOW.add((pool_name, f"{key[0]}->{key[1]}"))


def shadow_comparison_snapshot() -> dict[str, list[tuple[str, int]]]:
    """Per-pool actual→value-pick transition counts for /status."""
    grouped: dict[str, list[tuple[str, int]]] = {}
    for (pool, pair), count in _SHADOW.items():
        grouped.setdefault(pool, []).append((pair, count))
    return grouped


def reset_shadow_state_for_tests() -> None:
    with _SHADOW_LOCK:
        _SHADOW._counts.clear()


# --- Scoring -------------------------------------------------------------

@dataclass(frozen=True)
class RouteScore:
    """The computed value-routing terms for one candidate."""

    provider: str
    model: str
    quality: float
    intel: float
    blended_cost: float
    value: float
    value_norm: float
    speed_norm: float
    final: float
    pacing_factor: float


def score_candidates(
    *,
    pool_name: str,
    candidates: list[tuple[str, str]],
    quality_scores: dict[tuple[str, str], float],
    latency_ms: dict[tuple[str, str], float | None],
    plans: dict[str, ProviderPlan],
    intel: IntelPrior,
    ranks_by_slug: dict[str, int | None],
    cost_model: RouteCostModel,
    input_fraction: float,
    pacer: BudgetPacer | None,
    quality_weight: float = DEFAULT_QUALITY_WEIGHT,
    value_weight: float = DEFAULT_VALUE_WEIGHT,
    speed_weight: float = DEFAULT_SPEED_WEIGHT,
) -> list[RouteScore]:
    """Score every candidate; order-independent, returns unsorted scores."""
    scores: list[RouteScore] = []
    latencies = [
        latency_ms.get(pair)
        for pair in candidates
        if latency_ms.get(pair) is not None
    ]
    max_latency = max(latencies) if latencies else None
    raw: list[tuple[tuple[str, str], float, float, float, float, float, float]] = []
    for pair in candidates:
        provider, model = pair
        plan = plans.get(provider) or ProviderPlan(provider=provider)
        quality = float(quality_scores.get(pair, 40.0))
        intel_prior = intel.prior_by_slug(normalize_slug(model), ranks_by_slug)
        if cost_model.has_pricing(provider, model):
            cost = cost_model.blended_cost(
                provider,
                model,
                multiplier=plan.multiplier,
                input_fraction=input_fraction,
            )
            value = intel_prior / max(cost, 1e-6)
        else:
            # Unknown pricing cannot substantiate value: the route gets no
            # value credit at all so it never wins on the value term over a
            # priced route (mirrors the unpriced-entry demotion rule).
            cost = float("inf")
            value = 0.0
        latency = latency_ms.get(pair)
        speed = (
            1.0 / (1.0 + (latency or 0.0) / 1500.0)
            if latency is not None
            else 0.5
        )
        pacing = pacer.decay(plan) if pacer else 1.0
        raw.append((pair, quality, intel_prior, cost, value, speed * pacing, pacing))
    # Normalize value and speed into 0-100 bands across this candidate set.
    values = [entry[4] for entry in raw]
    speeds = [entry[5] for entry in raw]
    min_value = min(values) if values else 0.0
    max_value = max(values) if values else 0.0
    value_span = max(max_value - min_value, 1e-6)
    min_speed = min(speeds) if speeds else 0.0
    speed_span = max((max(speeds) - min_speed) if speeds else 0.0, 1e-6)
    for pair, quality, intel_val, cost, value, speed, pacing_factor in raw:
        value_norm = (
            100.0 * math.log1p(max(value - min_value, 0.0)) / math.log1p(value_span)
            if value_span > 1.0
            else 50.0
        )
        speed_norm = 100.0 * (speed - min_speed) / speed_span
        final = (
            quality_weight * quality
            + value_weight * value_norm
            + speed_weight * speed_norm
        )
        scores.append(
            RouteScore(
                provider=pair[0],
                model=pair[1],
                quality=quality,
                intel=intel_val,
                blended_cost=cost,
                value=value,
                value_norm=value_norm,
                speed_norm=speed_norm,
                final=final,
                pacing_factor=pacing_factor,
            )
        )
    return scores


__all__ = [
    "BudgetPacer",
    "Counter",
    "IntelPrior",
    "ProviderPlan",
    "RouteCostModel",
    "RouteScore",
    "UsageMix",
    "load_provider_plans",
    "record_shadow_comparison",
    "reset_shadow_state_for_tests",
    "score_candidates",
    "shadow_comparison_snapshot",
    "value_routing_mode",
]
