"""Prometheus metrics exporter for tusker-gateway.

Implements the minimal Prometheus text exposition format ourselves so we
don't take a dependency on `prometheus_client` (which is heavy and pulls in
twisted/asgiref). Our implementation covers the subset we actually emit.

Endpoint shape:
    GET /metrics
        Returns a UTF-8 text/plain body with `Content-Type: text/plain;
        version=0.0.4; charset=utf-8` (Prometheus spec).

Metric catalogue:
    tusker_requests_total{pool,provider,model,status}        counter
    tusker_tokens_total{pool,provider,model,direction}       counter
    tusker_request_duration_seconds{pool,provider,model}      histogram
    tusker_cache_hits_total                                    counter
    tusker_cache_misses_total                                  counter
    tusker_cache_writes_total                                  counter
    tusker_cache_evictions_total                               counter
    tusker_semantic_cache_hits_total                            counter
    tusker_semantic_cache_misses_total                          counter
    tusker_semantic_cache_writes_total                          counter
    tusker_semantic_cache_evictions_total                       counter
    tusker_semantic_cache_errors_total                          counter
    tusker_semantic_cache_skips_total                           counter
    tusker_budget_blocks_total{kind}                          counter
    tusker_budget_records_total                                counter
    tusker_budget_refunds_total                                counter
    tusker_guardrail_blocks_total{kind}                        counter
    tusker_pool_candidates{pool,state}                         gauge
    tusker_cooldowns_active{provider,model}                    gauge
    tusker_rtk_blocks_total{filter,outcome}                   counter
    tusker_rtk_bytes_saved_total                               counter
    tusker_rtk_calls_total{outcome}                           counter

We deliberately scope labels to keep cardinality bounded:
    - `pool` is one of {code, privacy, premium, swarm, passthrough, rerank}
    - `provider` is one of the configured providers (~10 today)
    - `model` is the (provider, model) pair as a single string label.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable


# Buckets chosen for LLM latencies: 50ms .. 60s, log-spaced-ish but skewed
# toward the long tail because streaming responses dominate wall time.
DEFAULT_LATENCY_BUCKETS: tuple[float, ...] = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0,
)


@dataclass
class Counter:
    """Prometheus counter."""
    name: str
    help: str
    label_names: tuple[str, ...] = ()
    _values: dict[tuple[str, ...], float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def get(self, labels: dict[str, str] | None = None) -> float:
        return self._values.get(self._key(labels), 0.0)

    def _key(self, labels: dict[str, str] | None) -> tuple[str, ...]:
        if not self.label_names:
            return ()
        labels = labels or {}
        return tuple(str(labels.get(n, "")) for n in self.label_names)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} counter"
        with self._lock:
            items = list(self._values.items())
        if not items:
            # Emit zero-valued samples for declared labels if any.
            # For counters without label names, emit nothing (Prometheus OK).
            return
        for key, value in items:
            if self.label_names:
                pairs = ",".join(
                    f'{n}="{_escape(v)}"' for n, v in zip(self.label_names, key)
                )
                yield f"{self.name}{{{pairs}}} {_format_float(value)}"
            else:
                yield f"{self.name} {_format_float(value)}"


@dataclass
class Gauge:
    """Prometheus gauge."""
    name: str
    help: str
    label_names: tuple[str, ...] = ()
    _values: dict[tuple[str, ...], float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, labels: dict[str, str] | None, value: float) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, labels: dict[str, str] | None, amount: float = 1.0) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, labels: dict[str, str] | None, amount: float = 1.0) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) - amount

    def get(self, labels: dict[str, str] | None = None) -> float:
        return self._values.get(self._key(labels), 0.0)

    def _key(self, labels: dict[str, str] | None) -> tuple[str, ...]:
        if not self.label_names:
            return ()
        labels = labels or {}
        return tuple(str(labels.get(n, "")) for n in self.label_names)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} gauge"
        with self._lock:
            items = list(self._values.items())
        for key, value in items:
            if self.label_names:
                pairs = ",".join(
                    f'{n}="{_escape(v)}"' for n, v in zip(self.label_names, key)
                )
                yield f"{self.name}{{{pairs}}} {_format_float(value)}"
            else:
                yield f"{self.name} {_format_float(value)}"


@dataclass
class Histogram:
    """Prometheus histogram with bounded bucket count.

    We use fixed buckets rather than the `prometheus_client` exponential
    approach so the file size stays predictable.
    """
    name: str
    help: str
    label_names: tuple[str, ...] = ()
    buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS
    _sums: dict[tuple[str, ...], float] = field(default_factory=dict)
    _counts: dict[tuple[str, ...], int] = field(default_factory=dict)
    _bucket_counts: dict[tuple[str, ...], dict[float, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._key(labels)
        with self._lock:
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._counts[key] = self._counts.get(key, 0) + 1
            bc = self._bucket_counts.setdefault(key, {b: 0 for b in self.buckets})
            for b in self.buckets:
                if value <= b:
                    bc[b] = bc.get(b, 0) + 1

    def _key(self, labels: dict[str, str] | None) -> tuple[str, ...]:
        if not self.label_names:
            return ()
        labels = labels or {}
        return tuple(str(labels.get(n, "")) for n in self.label_names)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} histogram"
        with self._lock:
            keys = list(self._sums.keys()) if self._sums else [()]
        for key in keys:
            label_pairs = ""
            if self.label_names:
                pairs = ",".join(
                    f'{n}="{_escape(v)}"' for n, v in zip(self.label_names, key)
                )
                label_pairs = pairs + "," if pairs else ""
            for b in self.buckets:
                with self._lock:
                    count = self._bucket_counts.get(key, {}).get(b, 0)
                bucket_label = f'le="{_format_float(b)}"'
                yield f'{self.name}_bucket{{{label_pairs}{bucket_label}}} {count}'
            # +Inf bucket
            with self._lock:
                count = self._counts.get(key, 0)
            yield f'{self.name}_bucket{{{label_pairs}le="+Inf"}} {count}'
            with self._lock:
                s = self._sums.get(key, 0.0)
                c = self._counts.get(key, 0)
            yield f'{self.name}_sum{{{label_pairs.rstrip(",")}}} {_format_float(s)}'
            yield f'{self.name}_count{{{label_pairs.rstrip(",")}}} {c}'


def _escape(s: Any) -> str:
    """Escape per Prometheus exposition spec."""
    s = str(s)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_float(v: float) -> str:
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


class MetricsRegistry:
    """Container for all metric instances + rendering."""

    CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

    def __init__(self) -> None:
        self.requests_total = Counter(
            "tusker_requests_total",
            "Total chat completion requests by outcome",
            ("pool", "provider", "model", "status"),
        )
        self.tokens_total = Counter(
            "tusker_tokens_total",
            "Tokens processed by direction (prompt|completion)",
            ("pool", "provider", "model", "direction"),
        )
        self.request_duration = Histogram(
            "tusker_request_duration_seconds",
            "End-to-end chat completion latency in seconds",
            ("pool", "provider", "model"),
        )
        self.cache_hits = Counter("tusker_cache_hits_total", "Cache hits")
        self.cache_misses = Counter("tusker_cache_misses_total", "Cache misses")
        self.cache_writes = Counter("tusker_cache_writes_total", "Cache writes")
        self.cache_evictions = Counter(
            "tusker_cache_evictions_total", "Cache evictions (expired or LRU)"
        )
        self.semantic_cache_hits = Counter(
            "tusker_semantic_cache_hits_total", "Semantic cache hits"
        )
        self.semantic_cache_misses = Counter(
            "tusker_semantic_cache_misses_total", "Semantic cache misses"
        )
        self.semantic_cache_writes = Counter(
            "tusker_semantic_cache_writes_total", "Semantic cache writes"
        )
        self.semantic_cache_evictions = Counter(
            "tusker_semantic_cache_evictions_total", "Semantic cache evictions"
        )
        self.semantic_cache_errors = Counter(
            "tusker_semantic_cache_errors_total", "Semantic cache errors"
        )
        self.semantic_cache_skips = Counter(
            "tusker_semantic_cache_skips_total", "Semantic cache skips"
        )
        self.budget_blocks = Counter(
            "tusker_budget_blocks_total",
            "Requests blocked because a budget cap was exceeded",
            ("kind",),  # "daily" | "monthly" | "pool:<name>" | "global_daily"
        )
        self.budget_records = Counter(
            "tusker_budget_records_total", "Token usage records committed"
        )
        self.budget_refunds = Counter(
            "tusker_budget_refunds_total", "Token refunds for failed provider calls"
        )
        self.guardrail_blocks = Counter(
            "tusker_guardrail_blocks_total",
            "Requests blocked by guardrail checks",
            ("kind",),
        )
        # RTK (token-saver) observability.
        #
        # tusker_rtk_blocks_total{filter,outcome}
        #   filter  = the RTK filter that fired (git-diff, cargo-test, ...)
        #   outcome = "compressed" | "no_savings" | "skipped_short"
        #             | "skipped_too_large" | "no_match"
        #   Lets us see which filters actually pay off in production and
        #   how often a candidate was rejected by the savings threshold.
        #   Bounded cardinality: 8 filters × 5 outcomes = 40 series.
        self.rtk_blocks = Counter(
            "tusker_rtk_blocks_total",
            "RTK compress_text invocations by filter and outcome",
            ("filter", "outcome"),
        )
        # tusker_rtk_bytes_saved_total
        #   Sum of (input_bytes - output_bytes) across all compress_text
        #   calls that actually produced savings. Plot as a rate to see
        #   real-world savings over time.
        self.rtk_bytes_saved = Counter(
            "tusker_rtk_bytes_saved_total",
            "Total input bytes removed by RTK compression",
        )
        # tusker_rtk_calls_total{outcome}
        #   outcome = "compressed" | "no_match" | "skipped_short"
        #             | "skipped_too_large"
        #   Coarse-grained: one counter per content block. Useful for
        #   answer-ratio dashboards ("of 1k tool outputs, how many were
        #   actually compressed?").
        self.rtk_calls = Counter(
            "tusker_rtk_calls_total",
            "RTK compress_text invocations by high-level outcome",
            ("outcome",),
        )
        self.pool_candidates = Gauge(
            "tusker_pool_candidates",
            "Current number of candidates per pool, partitioned by validity",
            ("pool", "state"),  # "valid" | "invalid"
        )
        self.cooldowns_active = Gauge(
            "tusker_cooldowns_active",
            "Currently active (provider, model) cooldowns",
            ("provider", "model"),
        )
        self.start_time = time.time()
        # Process-level metadata
        self._meta: list[tuple[str, str]] = []

    def set_pool_candidates(self, pool: str, valid: int, invalid: int) -> None:
        self.pool_candidates.set({"pool": pool, "state": "valid"}, valid)
        self.pool_candidates.set({"pool": pool, "state": "invalid"}, invalid)

    def set_cooldowns_active(self, items: Iterable[tuple[str, str]]) -> None:
        # Reset and rewrite — gauge semantics, last-write-wins.
        self.cooldowns_active._values.clear()  # noqa: SLF001 — internal use
        for provider, model in items:
            self.cooldowns_active.set({"provider": provider, "model": model}, 1)

    def add_meta(self, name: str, value: str) -> None:
        self._meta.append((name, value))

    def render(self) -> str:
        parts: list[str] = []
        # Process info as comments (Prometheus convention)
        for name, value in self._meta:
            parts.append(f"# Tusker {name}={value}")
        parts.append(f"# Tusker process_start_ts={int(self.start_time)}")
        # Render every metric
        for m in (
            self.requests_total, self.tokens_total, self.request_duration,
            self.cache_hits, self.cache_misses, self.cache_writes, self.cache_evictions,
            self.semantic_cache_hits, self.semantic_cache_misses,
            self.semantic_cache_writes, self.semantic_cache_evictions,
            self.semantic_cache_errors, self.semantic_cache_skips,
            self.budget_blocks, self.budget_records, self.budget_refunds, self.guardrail_blocks,
            self.pool_candidates, self.cooldowns_active,
            self.rtk_blocks, self.rtk_bytes_saved, self.rtk_calls,
        ):
            for line in m.render():
                parts.append(line)
        return "\n".join(parts) + "\n"


__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "MetricsRegistry",
    "DEFAULT_LATENCY_BUCKETS",
]
