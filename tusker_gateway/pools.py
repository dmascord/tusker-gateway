"""Provider pools: candidate lists, selection, session stickiness.

A PoolConfig defines which (provider, model) pairs participate in a virtual
role alias (hermes-code, hermes-privacy, hermes-premium, hermes-swarm).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY, PoolConfig
from tusker_gateway.cooldown import CooldownTracker, global_tracker
from tusker_gateway.heavyweight import is_heavyweight
from tusker_gateway.quality import QualityDB

logger = logging.getLogger(__name__)


# Pools that are considered "premium" tiers — heavyweight models are
# kept for these (mirrors hermes-agent's `hermes-premium` semantics).
# Cheap-tier pools (`hermes-code`, `hermes-privacy`) drop heavyweights.
PREMIUM_POOLS: frozenset[str] = frozenset({"premium", "swarm"})


def _validate_providers(specs: list["ModelSpec"]) -> list[str]:
    """Return warnings for any model whose provider is not in the registry.

    Logging happens once per (provider, model) pair to avoid duplicate spam.
    """
    warnings: list[str] = []
    for s in specs:
        if s.provider not in DEFAULT_PROVIDER_REGISTRY:
            warnings.append(f"unknown provider '{s.provider}' for model '{s.model}'")
    return warnings


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
        # Per-entry `heavyweight` override; fall back to slug-based classifier.
        # This means operators don't need to keep the pool JSON in sync when
        # OpenAI/Copilot ship new heavyweight slugs — the slug set catches them.
        hw = data.get("heavyweight")
        if hw is None:
            hw = is_heavyweight(model)
        else:
            hw = bool(hw)
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
    # Optional catalog registry — when set, PoolManager.extend_pools_with_catalog()
    # merges catalog-known models into the allowlist. See catalog.py.
    catalog_registry: object | None = None
    # pool_name -> set of (provider, model) pairs auto-added by
    # extend_pools_with_free_catalog(). Tracked separately from
    # static allowlist so we can prune auto-added entries when
    # they stop being free (e.g. stealth/ox-alpha goes paid),
    # while keeping operator-curated entries untouched.
    auto_added: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    # pool_name -> set of (provider, model) pairs from the original
    # TUSKER_POOL_* config, snapshotted at startup. Used by
    # extend_pools_with_free_catalog() to distinguish operator-curated
    # entries (never pruned) from auto-added ones (pruned when they
    # stop being free).
    _original_static: dict[str, frozenset[tuple[str, str]]] = field(default_factory=dict)
    _stickiness: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    STICKINESS_TTL = 3600.0  # 1 hour
    def __post_init__(self):
        self.pools = dict(self.config.get("pools", {}))
        self._quality = QualityDB(self.config["quality_db_path"])
        self._cooldowns = global_tracker()
        # Build model lists from pool configs
        for name, pool in self.pools.items():
            # Snapshot the operator-curated entries BEFORE any catalog
            # merge so auto_free can distinguish static from auto-added.
            self._original_static[name] = frozenset(
                (m.get("provider", ""), m.get("model", ""))
                for m in pool.models
            )
            specs = [
                ModelSpec.from_dict(m, default_window=pool.context_window, zdr=pool.zdr)
                for m in pool.models
            ]
            self.models[name] = specs
            # Warn about unknown providers once at startup
            warnings = _validate_providers(specs)
            for w in warnings:
                logger.warning("pool '%s': %s — will be skipped during selection", name, w)
    def pool_keeps_heavyweight(self, pool_name: str) -> bool:
        """Return True if the named pool should keep heavyweight candidates.

        Cheap-tier pools (code, privacy) drop heavyweights.
        Premium-tier pools (premium, swarm) keep them.
        """
        return pool_name in PREMIUM_POOLS

    def static_allowlist(self, pool_name: str) -> set[tuple[str, str]]:
        """Return the set of (provider, model) pairs explicitly listed in
        the static pool config for ``pool_name``.

        Used as the allowlist when merging catalog entries — operators
        opt in to catalog models by listing them in TUSKER_POOL_*.
        """
        pool = self.pools.get(pool_name)
        if not pool:
            return set()
        return {
            (m.get("provider", ""), m.get("model", ""))
            for m in pool.models
        }

    def extend_pools_with_catalog(self) -> dict[str, int]:
        """Merge catalog-known models into every pool.

        For each (provider, model) in the static allowlist, if the catalog
        also knows about that (provider, model) pair, ensure the pool has
        a fresh entry for it. Useful when an upstream model is renamed
        and the operator updates the static entry but the catalog still
        has the old slug — the catalog confirms the model is live.

        Returns a mapping of pool_name -> number of catalog-confirmed
        entries.
        """
        if self.catalog_registry is None:
            return {}
        confirmed: dict[str, int] = {}
        for pool_name in self.pools:
            allowlist = self.static_allowlist(pool_name)
            count = 0
            for provider, model in allowlist:
                if not provider or not model:
                    continue
                catalog_models = self.catalog_registry.known_models(provider)
                if catalog_models is None:
                    continue  # provider not catalog-covered
                if model not in catalog_models:
                    continue  # catalog doesn't have it; static stays
                count += 1
            confirmed[pool_name] = count
        logger.info("catalog confirmed %s pool entries", confirmed)
        return confirmed
    def extend_pools_with_free_catalog(self) -> dict[str, list[str]]:
        """Auto-promote free upstream models into pools marked auto_free=True.

        For each pool whose ``PoolConfig.auto_free`` is set, scan the
        catalog for ``(provider, model)`` pairs whose upstream tier is
        free for the configured API key. These are merged into the
        pool's static ``models`` list, and ``self.models[name]`` is
        rebuilt via ``reload_all_pools()`` so the new candidates
        participate in selection immediately.

        Free-model discovery differs per upstream:
        - openrouter: ``pricing.prompt == "0" and pricing.completion == "0"``
          (their explicit free-tier signal).
        - opencode-zen / opencode-go: ``/v1/models`` is already
          key-filtered (the response omits paid models the key can't
          access), so the entire catalog is treated as free-for-this-key.

        The merge is idempotent across catalog refreshes:
        - Free models newly appearing on the upstream are added.
        - Free models that have since gone paid are removed from the
          pool (tracked in ``self.auto_added`` so we don't conflate
          with operator-curated entries).
        - Models already in the static allowlist stay where they are.

        Returns a mapping of pool_name -> the list of (provider, model)
        slugs currently held by the pool after the merge (post-reload).
        """
        if self.catalog_registry is None:
            return {}
        changed = False
        for pool_name, pool in self.pools.items():
            if not pool.auto_free:
                continue
            # Static = entries from TUSKER_POOL_* that the operator set
            # at startup. Frozen at __post_init__ time so we can tell
            # "operator put it here" apart from "auto_free put it here".
            static_pairs: set[tuple[str, str]] = set(
                self._original_static.get(pool_name, frozenset())
            )
            # Auto-added = entries we promoted on a previous pass.
            previously_auto = self.auto_added.get(pool_name, set())

            free_pairs: set[tuple[str, str]] = set()
            for prov, mode in (
                ("openrouter", "pricing"),
                ("opencode-zen", "all"),
                ("opencode-go", "all"),
            ):
                entries = self.catalog_registry.entries_for(prov)
                if not entries:
                    continue
                for e in entries:
                    if mode == "pricing":
                        if e.cost_input is None or e.cost_input > 0:
                            continue
                        if e.cost_output is None or e.cost_output > 0:
                            continue
                    free_pairs.add((e.provider, e.model))

            # Final set = static entries + (auto-added entries that are
            # still free). When a previously-auto entry disappears from
            # the free set, it's pruned here. When a new free entry
            # appears, it's added.
            desired_auto = previously_auto & free_pairs
            new_auto = free_pairs - static_pairs
            desired = static_pairs | desired_auto | new_auto
            current = static_pairs | previously_auto
            if desired == current:
                # Still idempotent: refresh tracked set without touching pool.
                self.auto_added[pool_name] = (previously_auto & free_pairs) | new_auto
                continue

            new_models = [
                m for m in pool.models
                if (m.get("provider", ""), m.get("model", "")) in static_pairs
            ]
            # Re-add auto entries that are still free, then any newly free.
            surviving_auto = sorted(previously_auto & free_pairs)
            new_auto_sorted = sorted(new_auto)
            new_models.extend({"provider": p, "model": m} for (p, m) in surviving_auto)
            new_models.extend({"provider": p, "model": m} for (p, m) in new_auto_sorted)
            pool.models = new_models
            self.auto_added[pool_name] = (previously_auto & free_pairs) | new_auto
            changed = True
            logger.info(
                "auto_free pool '%s': %d free catalog entries (was %d auto)",
                pool_name, len(free_pairs), len(previously_auto),
            )
        if changed:
            self.reload_all_pools()

    def reload_all_pools(self) -> None:
        """Rebuild ModelSpec lists from current pool configs.

        Called after a catalog refresh so the pool reflects any model
        additions made by the operator in TUSKER_POOL_*. Does NOT add
        catalog-only models — those still need to be in the static
        allowlist to be picked up.
        """
        for name, pool in self.pools.items():
            specs = [
                ModelSpec.from_dict(m, default_window=pool.context_window, zdr=pool.zdr)
                for m in pool.models
            ]
            self.models[name] = specs

    def select(
        self,
        pool_name: str,
        *,
        context_tokens: int = 0,
        session_id: str | None = None,
        preferred: str | None = None,
        heavyweight_ok: bool | None = None,
        excluded: set[tuple[str, str]] | None = None,
    ) -> tuple[str, str] | None:
        """Select the best (provider, model) from a pool.

        Failed candidates can be excluded for request-level fallback.

        ``heavyweight_ok`` defaults to the pool's tier: cheap-tier pools
        (code, privacy) drop heavyweights, premium-tier pools (premium,
        swarm) keep them. Pass an explicit True/False to override.
        """
        excluded = excluded or set()
        specs = self.models.get(pool_name, [])
        if not specs:
            return None

        # Resolve heavyweight gate from pool tier if not explicitly set.
        if heavyweight_ok is None:
            heavyweight_ok = self.pool_keeps_heavyweight(pool_name)

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
                        # Don't return a sticky heavyweight if the pool
                        # no longer allows it (config changed mid-session).
                        if not heavyweight_ok and s.heavyweight:
                            self._stickiness.pop(key, None)
                            break
                        return prev
        # 2. Filter candidates by context window, cooldown, and request-level exclusions
        candidates: list[ModelSpec] = []
        for s in specs:
            if (s.provider, s.model) in excluded:
                continue
            # Skip providers that are not in the registry — avoids ProviderError cascade
            if s.provider not in DEFAULT_PROVIDER_REGISTRY:
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
            default_score=100.0,
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
            valid = [s for s in specs if s.provider in DEFAULT_PROVIDER_REGISTRY]
            invalid = [s for s in specs if s.provider not in DEFAULT_PROVIDER_REGISTRY]
            result[name] = {
                "models": len(specs),
                "valid_candidates": len(valid),
                "invalid_candidates": len(invalid),
                "invalid_entries": [
                    {"provider": s.provider, "model": s.model}
                    for s in invalid
                ],
                "candidates": [
                    {"provider": s.provider, "model": s.model, "context_window": s.context_window}
                    for s in valid
                ],
            }
        return result
