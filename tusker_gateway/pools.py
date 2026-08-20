"""Provider pools: candidate lists, selection, session stickiness.

A PoolConfig defines which (provider, model) pairs participate in a virtual
role alias (hermes-code, hermes-privacy, hermes-premium, hermes-swarm).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from tusker_gateway.config import PoolConfig
from tusker_gateway.cooldown import CooldownTracker, global_tracker
from tusker_gateway.quality import QualityDB


@dataclass
class ModelSpec:
    provider: str
    model: str
    context_window: int
    heavyweight: bool
    zdr_ok: bool  # allowed in ZDR (privacy) pools

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_window: int = 128_000, zdr: bool = False) -> "ModelSpec":
        provider = data["provider"]
        model = data["model"]
        ctx = int(data.get("context_window", default_window))
        hw = bool(data.get("heavyweight", False))
        return cls(
            provider=provider,
            model=model,
            context_window=ctx,
            heavyweight=hw,
            zdr_ok=zdr and not hw,  # exclude heavyweights in ZDR
        )


@dataclass
class PoolManager:
    """Central pool manager: models, selection, stickiness."""

    config: dict[str, Any]
    pools: dict[str, PoolConfig] = field(default_factory=dict)
    models: dict[str, list[ModelSpec]] = field(default_factory=dict)
    _quality: QualityDB | None = None
    _cooldowns: CooldownTracker | None = None

    # Session stickiness: (session_id, pool_name) → (provider, model)
    _stickiness: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    STICKINESS_TTL = 3600.0  # 1 hour

    def __post_init__(self):
        self.pools = dict(self.config.get("pools", {}))
        self._quality = QualityDB(self.config["quality_db_path"])
        self._cooldowns = global_tracker()
        # Build model lists from pool configs
        for name, pool in self.pools.items():
            self.models[name] = [
                ModelSpec.from_dict(m, default_window=pool.context_window, zdr=pool.zdr)
                for m in pool.models
            ]

    def select(
        self,
        pool_name: str,
        *,
        context_tokens: int = 0,
        session_id: str | None = None,
        preferred: str | None = None,
        heavyweight_ok: bool = False,
        excluded: set[tuple[str, str]] | None = None,
    ) -> tuple[str, str] | None:
        """Select the best (provider, model) from a pool.

        Failed candidates can be excluded for request-level fallback.
        """
        excluded = excluded or set()
        specs = self.models.get(pool_name, [])
        if not specs:
            return None

        # 1. Session stickiness
        if session_id:
            key = (session_id, pool_name)
            if key in self._stickiness:
                prev = self._stickiness[key]
                for s in specs:
                    if (s.provider, s.model) == prev:
                        if (s.provider, s.model) in excluded:
                            self._stickiness.pop(key, None)
                            break
                        if context_tokens > 0 and s.context_window < context_tokens:
                            # Context doesn't fit; clear stickiness
                            self._stickiness.pop(key, None)
                            break
                        return prev
        # 2. Filter candidates by context window, cooldown, and request-level exclusions
        candidates: list[ModelSpec] = []
        for s in specs:
            if (s.provider, s.model) in excluded:
                continue
            if context_tokens > 0 and s.context_window < context_tokens:
                continue
            if not heavyweight_ok and s.heavyweight:
                continue
            if self._cooldowns.is_cooldown(s.provider, s.model):
                continue
            # ZDR: exclude providers in exclusion list
            pool_config = self.pools.get(pool_name)
            if pool_config and pool_config.zdr:
                if s.provider in self.config.get("excluded_providers", []):
                    continue
            candidates.append(s)

        if not candidates:
            return None

        # 3. Rank by quality and pick top
        quality_list = self._quality.rank(
            [(c.provider, c.model) for c in candidates],
            default_score=50.0,
        )

        # Pick top candidate
        if quality_list:
            best = quality_list[0]
            result = (best[0], best[1])
            if session_id:
                key = (session_id, pool_name)
                self._stickiness[key] = result
            return result

        # Fallback: pick first candidate
        result = (candidates[0].provider, candidates[0].model)
        if session_id:
            key = (session_id, pool_name)
            self._stickiness[key] = result
        return result

    def status(self) -> dict[str, Any]:
        """Return pool status for /status endpoint."""
        result = {}
        for name, specs in self.models.items():
            result[name] = {
                "models": len(specs),
                "candidates": [
                    {"provider": s.provider, "model": s.model, "context_window": s.context_window}
                    for s in specs
                ],
            }
        return result
