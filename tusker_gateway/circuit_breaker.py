"""Per-provider circuit breaker.

Complements `cooldown.py`: cooldowns track explicit rate-limit responses
(429 + Retry-After). Circuit breakers track everything else that signals
"this provider is sick right now" — 5xx errors, transport failures, auth
failures. We don't know how long these take to heal, so we use a
fixed-duration open period followed by a half-open probe.

State machine:
    CLOSED ──[failures >= threshold]──> OPEN
    OPEN   ──[cooldown elapsed]────────> HALF_OPEN
    HALF_OPEN ──[probe success]────────> CLOSED
    HALF_OPEN ──[probe failure]────────> OPEN  (cooldown restarts)

Trigger policy (configurable per provider):
    - consecutive_failures: trip after N consecutive failures (default 5)
    - failure_ratio: trip when failures / total >= ratio over the rolling
      window (default 0.5 over last 20 calls)

Half-open semantics:
    Only ONE in-flight probe is allowed while half-open. All other requests
    are short-circuited. If the probe succeeds, full traffic resumes; if it
    fails, the breaker reopens for another cooldown period.

Storage:
    SQLite at the configured path. State per (provider, model) is small:
    `state`, `consecutive_failures`, `window_failures`, `window_total`,
    `window_started_at`, `opened_at`, `half_open_probe_inflight`.
    Failure events are NOT persisted (we only keep the rolling counter)
    because the breaker is a coarse-grained mechanism; precise per-call
    failure history belongs in `quality.py`.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


def _default_path() -> str:
    home = os.environ.get("HOME", "")
    if home:
        return os.path.join(home, ".hermes", "circuit.db")
    return "cache/circuit.db"


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerPolicy:
    """Per-provider breaker policy. Use the same policy for all providers by default."""
    consecutive_failures: int = 5
    window_size: int = 20
    failure_ratio: float = 0.5
    cooldown_secs: float = 60.0
    half_open_max_probes: int = 1


@dataclass
class BreakerConfig:
    enabled: bool = False
    path: str = field(default_factory=_default_path)
    policy: BreakerPolicy = field(default_factory=BreakerPolicy)
    # Per-provider overrides keyed by provider name; falls back to `policy`.
    overrides: dict[str, BreakerPolicy] = field(default_factory=dict)


@dataclass
class BreakerStats:
    trips: int = 0
    short_circuits: int = 0
    half_open_probes: int = 0
    half_open_successes: int = 0
    half_open_failures: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "trips": self.trips,
            "short_circuits": self.short_circuits,
            "half_open_probes": self.half_open_probes,
            "half_open_successes": self.half_open_successes,
            "half_open_failures": self.half_open_failures,
        }


@dataclass
class BreakerDecision:
    allowed: bool
    state: BreakerState
    reason: str | None = None


class CircuitBreaker:
    """Per-(provider, model) circuit breaker with SQLite-backed state."""

    def __init__(self, config: BreakerConfig):
        self._config = config
        self.stats = BreakerStats()
        if not config.enabled:
            return
        try:
            Path(config.path).parent.mkdir(parents=True, exist_ok=True)
            self._ensure_db()
        except (PermissionError, OSError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "circuit breaker disabled: cannot create %s: %s", config.path, exc
            )
            self._config.enabled = False

    def _ensure_db(self) -> None:
        with sqlite3.connect(self._config.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS breakers (
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    state TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    window_failures INTEGER NOT NULL DEFAULT 0,
                    window_total INTEGER NOT NULL DEFAULT 0,
                    window_started_at REAL NOT NULL DEFAULT 0,
                    opened_at REAL,
                    half_open_probe_inflight INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (provider, model)
                )
                """
            )
            conn.commit()

    def _policy_for(self, provider: str) -> BreakerPolicy:
        return self._config.overrides.get(provider, self._config.policy)

    # -- public API ------------------------------------------------------

    def check(self, provider: str, model: str) -> BreakerDecision:
        """Decide whether a request to (provider, model) should proceed."""
        if not self._config.enabled:
            return BreakerDecision(allowed=True, state=BreakerState.CLOSED)
        now = time.time()
        row = self._read(provider, model)
        state = BreakerState(row["state"]) if row else BreakerState.CLOSED
        opened_at = row["opened_at"] if row else None
        in_flight = bool(row["half_open_probe_inflight"]) if row else False

        if state == BreakerState.CLOSED:
            return BreakerDecision(allowed=True, state=BreakerState.CLOSED)

        if state == BreakerState.OPEN:
            cooldown = self._policy_for(provider).cooldown_secs
            if opened_at is None or (now - opened_at) >= cooldown:
                # Transition to HALF_OPEN; allow the caller to probe.
                self._update(provider, model,
                             state=BreakerState.HALF_OPEN.value,
                             half_open_probe_inflight=1)
                self.stats.half_open_probes += 1
                return BreakerDecision(
                    allowed=True,
                    state=BreakerState.HALF_OPEN,
                    reason="half_open_probe",
                )
            self.stats.short_circuits += 1
            remaining = max(0.0, cooldown - (now - (opened_at or now)))
            return BreakerDecision(
                allowed=False,
                state=BreakerState.OPEN,
                reason=f"circuit open ({remaining:.0f}s remaining)",
            )

        # HALF_OPEN
        if in_flight:
            self.stats.short_circuits += 1
            return BreakerDecision(
                allowed=False,
                state=BreakerState.HALF_OPEN,
                reason="half_open probe in flight",
            )
        # Allow exactly one more probe.
        self._update(provider, model, half_open_probe_inflight=1)
        self.stats.half_open_probes += 1
        return BreakerDecision(
            allowed=True,
            state=BreakerState.HALF_OPEN,
            reason="half_open_probe",
        )

    def record_success(self, provider: str, model: str) -> None:
        if not self._config.enabled:
            return
        row = self._read(provider, model)
        if row is None:
            # No state yet — nothing to do.
            return
        state = BreakerState(row["state"])
        policy = self._policy_for(provider)
        if state == BreakerState.HALF_OPEN:
            self.stats.half_open_successes += 1
            # Probe succeeded — back to CLOSED.
            self._update(
                provider, model,
                state=BreakerState.CLOSED.value,
                consecutive_failures=0,
                window_failures=0,
                window_total=0,
                window_started_at=time.time(),
                opened_at=None,
                half_open_probe_inflight=0,
            )
            return
        # CLOSED: roll the window.
        new_window = self._roll_window(row, success=True, policy=policy)
        self._update(
            provider, model,
            state=BreakerState.CLOSED.value,
            consecutive_failures=0,
            window_failures=new_window["window_failures"],
            window_total=new_window["window_total"],
            window_started_at=new_window["window_started_at"],
        )

    def record_failure(self, provider: str, model: str) -> None:
        if not self._config.enabled:
            return
        row = self._read(provider, model)
        policy = self._policy_for(provider)

        # Determine counters BEFORE writing.
        if row is None:
            # First-ever failure — open a fresh state row with counters at 1.
            consecutive = 1
            new_window = {"window_total": 1, "window_failures": 1, "window_started_at": time.time()}
            state = BreakerState.CLOSED
        else:
            state = BreakerState(row["state"])
            if state == BreakerState.HALF_OPEN:
                # Probe failed — re-open.
                self.stats.half_open_failures += 1
                self._update(
                    provider, model,
                    state=BreakerState.OPEN.value,
                    opened_at=time.time(),
                    half_open_probe_inflight=0,
                )
                return
            # CLOSED: bump counters, check trip conditions.
            consecutive = row["consecutive_failures"] + 1
            new_window = self._roll_window(row, success=False, policy=policy)

        should_trip = (
            consecutive >= policy.consecutive_failures
            or (
                new_window["window_total"] >= policy.window_size
                and (new_window["window_failures"] / new_window["window_total"]) >= policy.failure_ratio
            )
        )

        if should_trip:
            self.stats.trips += 1

        if row is None:
            # First failure — upsert (creates the row).
            self._upsert(
                provider, model,
                state=BreakerState.OPEN.value if should_trip else BreakerState.CLOSED.value,
                consecutive_failures=consecutive,
                window_failures=new_window["window_failures"],
                window_total=new_window["window_total"],
                window_started_at=new_window["window_started_at"],
                **({"opened_at": time.time()} if should_trip else {}),
            )
        elif should_trip:
            self._update(
                provider, model,
                state=BreakerState.OPEN.value,
                consecutive_failures=consecutive,
                window_failures=new_window["window_failures"],
                window_total=new_window["window_total"],
                window_started_at=new_window["window_started_at"],
                opened_at=time.time(),
            )
        else:
            self._update(
                provider, model,
                state=BreakerState.CLOSED.value,
                consecutive_failures=consecutive,
                window_failures=new_window["window_failures"],
                window_total=new_window["window_total"],
                window_started_at=new_window["window_started_at"],
            )

    def release_probe(self, provider: str, model: str) -> None:
        """Mark the in-flight half-open probe as completed (success OR failure).

        The caller is responsible for calling record_success/record_failure
        to actually update the state — this method only clears the
        in-flight flag so the next caller can probe.
        """
        if not self._config.enabled:
            return
        self._update(provider, model, half_open_probe_inflight=0)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return current breaker state for the dashboard."""
        if not self._config.enabled:
            return {}
        with sqlite3.connect(self._config.path) as conn:
            rows = conn.execute(
                "SELECT provider, model, state, opened_at, window_failures, window_total, consecutive_failures FROM breakers"
            ).fetchall()
        return {
            f"{p}|{m}": {
                "provider": p,
                "model": m,
                "state": s,
                "opened_at": oa,
                "window_failures": wf,
                "window_total": wt,
                "consecutive_failures": cf,
            }
            for p, m, s, oa, wf, wt, cf in rows
        }

    def stats_snapshot(self) -> dict[str, int]:
        return self.stats.snapshot()

    # -- internals -------------------------------------------------------

    def _read(self, provider: str, model: str) -> dict[str, Any] | None:
        with sqlite3.connect(self._config.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM breakers WHERE provider = ? AND model = ?",
                (provider, model),
            ).fetchone()
        return dict(row) if row else None

    def _upsert(self, provider: str, model: str, **fields: Any) -> None:
        cols = ["provider", "model", *fields.keys()]
        placeholders = ",".join("?" for _ in cols)
        values = [provider, model, *fields.values()]
        updates = ",".join(f"{k}=excluded.{k}" for k in fields.keys())
        with sqlite3.connect(self._config.path) as conn:
            conn.execute(
                f"INSERT INTO breakers ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(provider, model) DO UPDATE SET {updates}",
                values,
            )
            conn.commit()

    def _update(self, provider: str, model: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields.keys())
        values = [*fields.values(), provider, model]
        with sqlite3.connect(self._config.path) as conn:
            conn.execute(
                f"UPDATE breakers SET {sets} WHERE provider = ? AND model = ?",
                values,
            )
            conn.commit()

    def _roll_window(self, row: dict[str, Any], *, success: bool,
                     policy: BreakerPolicy) -> dict[str, int]:
        """Update rolling-window counters, resetting if the window has aged out."""
        now = time.time()
        window_total = row["window_total"] + 1
        window_failures = row["window_failures"] + (0 if success else 1)
        window_started_at = row["window_started_at"]
        # If window grew past `window_size`, the OLDEST call slides out.
        # We approximate: when window_total exceeds window_size, halve both
        # counters and reset started_at. This is intentionally coarse —
        # a real implementation would use a deque of timestamps.
        if window_total > policy.window_size:
            window_total = max(1, window_total // 2)
            window_failures = window_failures // 2
            window_started_at = now
        return {
            "window_total": window_total,
            "window_failures": window_failures,
            "window_started_at": window_started_at,
        }


def load_circuit_config_from_env(env: dict[str, str] | None = None) -> BreakerConfig:
    env = env if env is not None else os.environ
    enabled = env.get("TUSKER_CIRCUIT_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )
    return BreakerConfig(
        enabled=enabled,
        path=env.get("TUSKER_CIRCUIT_PATH") or _default_path(),
        policy=BreakerPolicy(
            consecutive_failures=int(env.get("TUSKER_CIRCUIT_CONSECUTIVE", "5")),
            window_size=int(env.get("TUSKER_CIRCUIT_WINDOW", "20")),
            failure_ratio=float(env.get("TUSKER_CIRCUIT_RATIO", "0.5")),
            cooldown_secs=float(env.get("TUSKER_CIRCUIT_COOLDOWN", "60")),
        ),
    )


__all__ = [
    "BreakerConfig",
    "BreakerDecision",
    "BreakerPolicy",
    "BreakerState",
    "BreakerStats",
    "CircuitBreaker",
    "load_circuit_config_from_env",
]
