"""Provider pools: candidate lists, selection, session stickiness.

A PoolConfig defines which (provider, model) pairs participate in a virtual
role alias (hermes-code, hermes-privacy, hermes-premium, hermes-swarm).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from tusker_gateway.catalog import advertised_input_modalities, advertised_tool_support
from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY, PoolConfig
from tusker_gateway.cooldown import CooldownTracker, global_tracker
from tusker_gateway.heavyweight import is_heavyweight
from tusker_gateway.quality import QualityDB

logger = logging.getLogger(__name__)


# Pools that are considered "premium" tiers — heavyweight models are
# kept for these (mirrors hermes-agent's `hermes-premium` semantics).
# Cheap-tier pools (`hermes-code`, `hermes-privacy`) drop heavyweights.
PREMIUM_POOLS: frozenset[str] = frozenset({"premium", "swarm"})

# Provider catalogs also expose models that are not response-generating chat
# models. Sending those through a general chat pool can return classifier prose
# (for example, "User Safety: safe") or route a request nondeterministically.
_SPECIAL_PURPOSE_MODEL_RE = re.compile(
    r"(?:^|[/._:-])(?:content[-_.]?safety|moderation|toxicity|"
    r"safety[-_.]?classifier|guard(?:rail)?)(?:$|[/._:-])",
    re.IGNORECASE,
)
_PROVIDER_ROUTER_MODELS: frozenset[tuple[str, str]] = frozenset({
    ("openrouter", "openrouter/free"),
    ("openrouter", "openrouter/auto"),
    ("openrouter", "free"),
    ("openrouter", "auto"),
})


def is_general_chat_model(provider: str, model: str) -> bool:
    """Return whether a model is suitable for a normal chat pool.

    This is deliberately conservative for catalog-discovered candidates:
    safety/moderation classifiers and provider-level routers are valid
    upstream products, but they are not valid general assistant backends.
    """
    normalized_provider = str(provider).strip().lower()
    normalized_model = str(model).strip().lower()
    if (normalized_provider, normalized_model) in _PROVIDER_ROUTER_MODELS:
        return False
    return _SPECIAL_PURPOSE_MODEL_RE.search(normalized_model) is None


def _validate_providers(specs: list["ModelSpec"]) -> list[str]:
    """Return warnings for any model whose provider is not in the registry.

    Logging happens once per (provider, model) pair to avoid duplicate spam.
    """
    warnings: list[str] = []
    for s in specs:
        if s.provider not in DEFAULT_PROVIDER_REGISTRY:
            warnings.append(f"unknown provider '{s.provider}' for model '{s.model}'")
    return warnings


def _split_unkeyed(
    specs: list[ModelSpec],
    provider_keys: dict[str, str],
) -> tuple[list[ModelSpec], list[tuple[ModelSpec, str]]]:
    """Soft-fail models whose provider has no usable credential.

    Bearer-kind providers need an entry in ``provider_api_keys``; without
    one every request to them would 500 at auth time, so we drop those
    candidates at pool-build time instead of preventing startup. OAuth,
    codex, and local providers are exempt — they fall back to token
    rotators or need no key at all.

    Returns ``(usable, skipped)`` where each skipped item is paired with
    a human-readable reason.
    """
    usable: list[ModelSpec] = []
    skipped: list[tuple[ModelSpec, str]] = []
    for s in specs:
        endpoint = DEFAULT_PROVIDER_REGISTRY.get(s.provider)
        if endpoint is not None and endpoint.kind == "bearer" and not provider_keys.get(s.provider.lower()):
            skipped.append((s, f"no API key configured for provider '{s.provider}'"))
            continue
        usable.append(s)
    return usable, skipped


@dataclass
class ModelSpec:
    provider: str
    model: str
    context_window: int
    heavyweight: bool
    zdr_ok: bool  # allowed in ZDR (privacy) pools
    input_modalities: frozenset[str] | None = None

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
        modalities = data.get("input_modalities")
        return cls(
            provider=provider,
            model=model,
            context_window=ctx,
            heavyweight=hw,
            zdr_ok=zdr and not hw,  # exclude heavyweights in ZDR
            input_modalities=(
                frozenset(str(modality) for modality in modalities)
                if modalities is not None
                else None
            ),
        )


@dataclass
class PoolManager:
    """Central pool manager: models, selection, stickiness."""

    config: dict[str, Any]
    pools: dict[str, PoolConfig] = field(default_factory=dict)
    models: dict[str, list[ModelSpec]] = field(default_factory=dict)
    # pool_name -> specs dropped because their provider has no API key.
    # Kept for /status visibility; never eligible for selection.
    unkeyed: dict[str, list[tuple[ModelSpec, str]]] = field(default_factory=dict)
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
            usable, unkeyed = _split_unkeyed(specs, self.config.get("provider_api_keys", {}))
            for s, reason in unkeyed:
                logger.warning(
                    "pool '%s': dropping %s '%s' — %s (add the key to the provider secret to enable)",
                    name, s.provider, s.model, reason,
                )
            self.unkeyed[name] = unkeyed
            self.models[name] = usable
            # Warn about unknown providers once at startup
            for w in _validate_providers(specs):
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
        """Auto-promote eligible catalog models into ``auto_free`` pools.

        OpenRouter contributes models whose input and output pricing is zero.
        OpenCode Zen/Go catalogs are key-filtered, so all advertised models are
        eligible. Xiaomi's authenticated catalog is also key-filtered, but its
        models are only added to non-ZDR cheap pools and pricing/slug-based
        heavyweights are excluded.

        Auto-added entries are tracked separately from operator-curated static
        entries so refreshes can add and prune models without disturbing the
        configured allowlist.
        """
        if self.catalog_registry is None:
            return {}

        changed = False
        for pool_name, pool in self.pools.items():
            if not pool.auto_free:
                continue

            static_pairs = set(self._original_static.get(pool_name, frozenset()))
            eligible: dict[tuple[str, str], dict[str, Any]] = {}
            excluded_special_models: list[str] = []

            for provider, mode in (
                ("openrouter", "pricing"),
                ("opencode-zen", "all"),
                ("opencode-go", "all"),
                ("xiaomi", "xiaomi"),
            ):
                if mode == "xiaomi" and (
                    pool.zdr or self.pool_keeps_heavyweight(pool_name)
                ):
                    continue
                entries = self.catalog_registry.entries_for(provider)
                if not entries:
                    continue

                for entry in entries:
                    if not is_general_chat_model(entry.provider, entry.model):
                        excluded_special_models.append(
                            f"{entry.provider}/{entry.model}"
                        )
                        continue
                    # Skip models that have permanently failed (401/403:
                    # WAF-blocked, agentic-harness-only, wrong-tier). They
                    # would otherwise be re-added and fail on every refresh.
                    # Excluding them here also prunes previously auto-added
                    # dead models (they leave `eligible` → `new_models`).
                    from tusker_gateway.cooldown import is_permanently_failed

                    if is_permanently_failed(entry.provider, entry.model):
                        continue
                    if mode == "pricing" and not (
                        entry.cost_input == 0.0 and entry.cost_output == 0.0
                    ):
                        continue

                    heavyweight = is_heavyweight(
                        entry.model,
                        cost_input=entry.cost_input,
                        cost_output=entry.cost_output,
                    )
                    if mode == "xiaomi" and heavyweight:
                        continue

                    model_data: dict[str, Any] = {
                        "provider": entry.provider,
                        "model": entry.model,
                    }
                    if mode == "xiaomi":
                        model_data["heavyweight"] = heavyweight
                    modalities = advertised_input_modalities(entry)
                    if modalities is not None:
                        model_data["input_modalities"] = sorted(modalities)
                    eligible[(entry.provider, entry.model)] = model_data

            desired_auto = set(eligible) - static_pairs
            static_models = [
                model for model in pool.models
                if (model.get("provider", ""), model.get("model", "")) in static_pairs
            ]
            auto_models = [eligible[pair] for pair in sorted(desired_auto)]
            new_models = static_models + auto_models

            if new_models != pool.models:
                pool.models = new_models
                changed = True
                logger.info(
                    "auto_free pool '%s': %d eligible catalog entries",
                    pool_name,
                    len(desired_auto),
                )
            if excluded_special_models:
                logger.info(
                    "auto_free pool '%s': excluded %d special-purpose models=%s",
                    pool_name,
                    len(excluded_special_models),
                    ",".join(sorted(excluded_special_models)[:12]),
                )
            self.auto_added[pool_name] = desired_auto

        if changed:
            self.reload_all_pools()

        return {
            pool_name: [f"{provider}/{model}" for provider, model in sorted(pairs)]
            for pool_name, pairs in self.auto_added.items()
        }


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
            usable, unkeyed = _split_unkeyed(specs, self.config.get("provider_api_keys", {}))
            for s, reason in unkeyed:
                logger.warning(
                    "pool '%s': dropping %s '%s' — %s (add the key to the provider secret to enable)",
                    name, s.provider, s.model, reason,
                )
            self.unkeyed[name] = unkeyed
            self.models[name] = usable

    def _catalog_entry_for(self, spec: ModelSpec) -> Any | None:
        """Return the cached catalog row for a model, when available."""
        registry = self.catalog_registry
        if registry is None:
            return None
        entries_for = getattr(registry, "entries_for", None)
        if not callable(entries_for):
            return None
        try:
            entries = entries_for(spec.provider)
        except Exception:
            return None
        if not entries:
            return None
        for entry in entries:
            if (
                getattr(entry, "provider", spec.provider) == spec.provider
                and getattr(entry, "model", "") == spec.model
            ):
                return entry
        return None

    def _model_capabilities(
        self,
        spec: ModelSpec,
        capability_cache: dict[tuple[str, str], tuple[frozenset[str] | None, bool | None]],
    ) -> tuple[frozenset[str] | None, bool | None]:
        """Resolve effective input/tool capabilities for a pool candidate."""
        key = (spec.provider, spec.model)
        cached = capability_cache.get(key)
        if cached is not None:
            return cached
        entry = self._catalog_entry_for(spec)
        modalities = spec.input_modalities
        if modalities is None:
            modalities = advertised_input_modalities(entry)
        tool_support = advertised_tool_support(entry)
        capability_cache[key] = (modalities, tool_support)
        return modalities, tool_support

    def select(
        self,
        pool_name: str,
        *,
        context_tokens: int = 0,
        session_id: str | None = None,
        preferred: str | None = None,
        heavyweight_ok: bool | None = None,
        excluded: set[tuple[str, str]] | None = None,
        required_input_modalities: set[str] | frozenset[str] | None = None,
        requires_tools: bool = False,
    ) -> tuple[str, str] | None:
        """Select the best (provider, model) from a pool.

        Failed candidates can be excluded for request-level fallback.

        ``heavyweight_ok`` defaults to the pool's tier: cheap-tier pools
        (code, privacy) drop heavyweights, premium-tier pools (premium,
        swarm) keep them. Pass an explicit True/False to override.

        ``required_input_modalities`` excludes models whose known input
        modalities do not cover the request. Models without modality metadata
        remain eligible for backward compatibility.

        ``requires_tools`` applies the same rule to catalog-advertised tool
        support. Models that explicitly lack tools are excluded; models with
        unknown capability metadata remain eligible for compatibility.
        """
        excluded = excluded or set()
        required_modalities = frozenset(required_input_modalities or ())
        specs = self.models.get(pool_name, [])
        if not specs:
            return None
        capability_cache: dict[
            tuple[str, str], tuple[frozenset[str] | None, bool | None]
        ] = {}

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
                        if not is_general_chat_model(s.provider, s.model):
                            self._stickiness.pop(key, None)
                            break
                        if context_tokens > 0 and s.context_window < context_tokens:
                            # Context doesn't fit; clear stickiness
                            self._stickiness.pop(key, None)
                            break
                        modalities, tool_support = self._model_capabilities(
                            s, capability_cache
                        )
                        if (
                            modalities is not None
                            and not required_modalities.issubset(modalities)
                        ):
                            self._stickiness.pop(key, None)
                            break
                        if requires_tools and tool_support is False:
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
        filtered_special_models: list[str] = []
        filtered_tool_models: list[str] = []
        filtered_modality_models: list[str] = []
        for s in specs:
            if (s.provider, s.model) in excluded:
                continue
            # Skip providers that are not in the registry — avoids ProviderError cascade
            if s.provider not in DEFAULT_PROVIDER_REGISTRY:
                continue
            if not is_general_chat_model(s.provider, s.model):
                filtered_special_models.append(f"{s.provider}/{s.model}")
                continue
            if context_tokens > 0 and s.context_window < context_tokens:
                continue
            if not heavyweight_ok and s.heavyweight:
                continue
            modalities, tool_support = self._model_capabilities(s, capability_cache)
            if modalities is not None and not required_modalities.issubset(modalities):
                filtered_modality_models.append(f"{s.provider}/{s.model}")
                continue
            if requires_tools and tool_support is False:
                filtered_tool_models.append(f"{s.provider}/{s.model}")
                continue
            if self._cooldowns.is_cooldown(s.provider, s.model):
                continue
            # ZDR: exclude providers in exclusion list
            pool_config = self.pools.get(pool_name)
            if pool_config and pool_config.zdr:
                if s.provider in self.config.get("excluded_providers", []):
                    continue
            candidates.append(s)

        if requires_tools and filtered_tool_models:
            logger.info(
                "pool '%s' capability filter tools=true filtered=%d models=%s",
                pool_name,
                len(filtered_tool_models),
                ",".join(filtered_tool_models[:12]),
            )
        if filtered_special_models:
            logger.info(
                "pool '%s' special-purpose filter filtered=%d models=%s",
                pool_name,
                len(filtered_special_models),
                ",".join(filtered_special_models[:12]),
            )
        if required_modalities and filtered_modality_models:
            logger.info(
                "pool '%s' capability filter input_modalities=%s filtered=%d models=%s",
                pool_name,
                "+".join(sorted(required_modalities)),
                len(filtered_modality_models),
                ",".join(filtered_modality_models[:12]),
            )

        if not candidates:
            return None

        # 3. Rank by quality and pick top
        quality_list = self._quality.rank(
            [(c.provider, c.model) for c in candidates],
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
                "unkeyed_entries": [
                    {"provider": s.provider, "model": s.model, "reason": reason}
                    for s, reason in self.unkeyed.get(name, [])
                ],
                "candidates": [
                    {"provider": s.provider, "model": s.model, "context_window": s.context_window}
                    for s in valid
                ],
            }
        return result
