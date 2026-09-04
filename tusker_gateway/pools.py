"""Provider pools: candidate lists, selection, session stickiness.

A PoolConfig defines which (provider, model) pairs participate in a virtual
role alias (hermes-code, hermes-privacy, hermes-premium, hermes-swarm).
"""
from __future__ import annotations

import logging
import math
import os
import random
import re
import time
import fnmatch
from dataclasses import dataclass, field
from typing import Any

from tusker_gateway.catalog import (
    advertised_input_modalities,
    advertised_output_modalities,
    advertised_tool_support,
)
from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY, PoolConfig
from tusker_gateway.cooldown import CooldownTracker, global_tracker
from tusker_gateway.heavyweight import is_heavyweight
from tusker_gateway.quality import QualityDB
from tusker_gateway.tool_capability import (
    ToolCapabilityDB,
    ToolCapabilityLevel,
    default_tool_capability_db_path,
)

logger = logging.getLogger(__name__)


def _normalise_input_modality(value: Any) -> str:
    """Normalize configured modality names to the catalog/DB spelling."""
    return str(value).strip().lower().replace("-", "_")


# Pools that are considered "premium" tiers — heavyweight models are
# kept for these (mirrors hermes-agent's `hermes-premium` semantics).
# Cheap-tier pools (`hermes-code`, `hermes-privacy`) drop heavyweights.
PREMIUM_POOLS: frozenset[str] = frozenset({"premium", "swarm"})

# Provider catalogs also expose models that are not response-generating chat
# models. Sending those through a general chat pool can return classifier prose
# (for example, "User Safety: safe") or route a request nondeterministically.
_SPECIAL_PURPOSE_MODEL_RE = re.compile(
    r"(?:^|[/._:-])(?:content[-_.]?safety|moderation|toxicity|"
    r"safety[-_.]?classifier|guard(?:rail)?|embed(?:ding)?s?|"
    r"rerank(?:er)?|whisper|transcrib(?:e|er|ing)?|tts|asr|"
    r"speech[-_.]?to[-_.]?text|text[-_.]?to[-_.]?speech|"
    r"native[-_.]?audio|live(?:[-_.]?preview)?|live[-_.]?audio|"
    r"audio[-_.]?(?:native|realtime)|realtime[-_.]?audio|"
    r"deep[-_.]?research|computer[-_.]?use|antigravity|aqa|"
    r"image[-_.]?(?:generation|gen)|text[-_.]?to[-_.]?image|"
    r"stable[-_.]?diffusion|dall[-_.]?e|imagen)(?:$|[/._:-])",
    re.IGNORECASE,
)
_PROVIDER_ROUTER_MODELS: frozenset[tuple[str, str]] = frozenset({
    ("openrouter", "openrouter/free"),
    ("openrouter", "openrouter/auto"),
    ("openrouter", "free"),
    ("openrouter", "auto"),
})


def _provider_registry(config: dict[str, Any]) -> dict[str, Any]:
    """Return the runtime provider registry, with the legacy default fallback."""
    configured = config.get("providers")
    if isinstance(configured, dict) and configured:
        return configured
    return DEFAULT_PROVIDER_REGISTRY


def is_general_chat_model(provider: str, model: str) -> bool:
    """Return whether a model is suitable for a normal chat pool.

    This is deliberately conservative for catalog-discovered candidates:
    safety/moderation classifiers, modality-specific endpoints such as
    Gemini Live/native-audio models, and provider-level routers are valid
    upstream products, but they are not valid general assistant backends.
    """
    normalized_provider = str(provider).strip().lower()
    normalized_model = str(model).strip().lower()
    if (normalized_provider, normalized_model) in _PROVIDER_ROUTER_MODELS:
        return False
    # Google's image-output model IDs use a bare ``-image`` suffix, which is
    # distinct from ordinary multimodal chat models whose slugs may merely
    # mention images. Keep this provider-specific to avoid filtering valid
    # OpenRouter/chat slugs such as a test or vision model named ``tool-image``.
    if normalized_provider == "google" and re.search(
        r"(?:^|[/._:-])image(?:$|[/._:-])", normalized_model,
    ):
        return False
    return _SPECIAL_PURPOSE_MODEL_RE.search(normalized_model) is None


def _validate_providers(
    specs: list["ModelSpec"],
    provider_registry: dict[str, Any] | None = None,
) -> list[str]:
    """Return warnings for any model whose provider is not in the registry.

    Logging happens once per (provider, model) pair to avoid duplicate spam.
    """
    known = provider_registry or DEFAULT_PROVIDER_REGISTRY
    warnings: list[str] = []
    for s in specs:
        if s.provider not in known:
            warnings.append(f"unknown provider '{s.provider}' for model '{s.model}'")
    return warnings


def _split_unkeyed(
    specs: list[ModelSpec],
    provider_keys: dict[str, str],
    provider_registry: dict[str, Any] | None = None,
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
    known = provider_registry or DEFAULT_PROVIDER_REGISTRY
    usable: list[ModelSpec] = []
    skipped: list[tuple[ModelSpec, str]] = []
    for s in specs:
        endpoint = known.get(s.provider)
        kind = getattr(endpoint, "kind", None)
        if isinstance(endpoint, dict):
            kind = endpoint.get("kind", endpoint.get("auth_type"))
        if endpoint is not None and kind == "bearer" and not provider_keys.get(s.provider.lower()):
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
    weight: float = 1.0  # relative weight for load-balanced selection (default: equal weight)
    input_modalities: frozenset[str] | None = None
    auto_discovered: bool = False
    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        default_window: int = 128_000,
        zdr: bool = False,
        provider_zdr_ok: bool | None = None,
    ) -> "ModelSpec":
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
        try:
            weight = float(data.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0  # fallback to equal weight
        if not math.isfinite(weight) or weight <= 0:
            weight = 1.0
        # ``provider_zdr_ok`` is optional for backwards-compatible direct
        # callers. PoolManager always supplies it so privacy policy is based
        # on the provider registry rather than only on the pool flag.
        provider_allowed = provider_zdr_ok if provider_zdr_ok is not None else True
        return cls(
            provider=provider,
            model=model,
            context_window=ctx,
            heavyweight=hw,
            zdr_ok=zdr and not hw and provider_allowed,
            weight=weight,
            input_modalities=(
                frozenset(
                    _normalise_input_modality(modality)
                    for modality in modalities
                    if _normalise_input_modality(modality)
                )
                if modalities is not None
                else None
            ),
            auto_discovered=bool(data.get("auto_discovered", False)),
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
    _tool_capabilities: ToolCapabilityDB | None = None
    _model_capability_db: Any | None = None
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
    _round_robin: dict[str, int] = field(default_factory=dict)
    _disabled_providers: frozenset[str] = field(
        init=False, default_factory=frozenset
    )
    STICKINESS_TTL = 3600.0  # 1 hour

    def _provider_zdr_ok(self, provider: str) -> bool:
        """Return the configured privacy/ZDR policy for one provider."""
        endpoint = self._providers.get(provider)
        if endpoint is None:
            return False
        if isinstance(endpoint, dict):
            return bool(endpoint.get("zdr_ok", False))
        return bool(getattr(endpoint, "zdr_ok", False))

    def _provider_is_disabled(self, provider: str) -> bool:
        """Return whether operator policy disables this provider for pools."""
        return str(provider).strip().lower().replace("_", "-") in self._disabled_providers

    def __post_init__(self):
        self.pools = dict(self.config.get("pools", {}))
        self._providers = _provider_registry(self.config)
        self._disabled_providers = frozenset(
            str(provider).strip().lower().replace("_", "-")
            for provider in self.config.get("disabled_providers", ())
            if str(provider).strip()
        )
        self._quality = QualityDB(self.config["quality_db_path"])
        self._tool_capabilities = ToolCapabilityDB(
            self.config.get("tool_capability_db_path")
            or default_tool_capability_db_path(self.config["quality_db_path"])
        )
        from tusker_gateway.model_capability import ModelCapabilityDB
        from tusker_gateway.model_capability import default_model_capability_db_path

        self._model_capability_db = ModelCapabilityDB(
            self.config.get("model_capability_db_path")
            or default_model_capability_db_path(self.config["quality_db_path"])
        )
        self._cooldowns = global_tracker()
        quality_path = self.config["quality_db_path"]
        if quality_path != ":memory:":
            try:
                from pathlib import Path

                from tusker_gateway.persistent_cooldown import PersistentCooldownStore

                cooldown_path = Path(quality_path).parent / "cooldowns.db"
                loaded_groups = PersistentCooldownStore(
                    db_path=cooldown_path
                ).hydrate_groups(self._cooldowns)
                if loaded_groups:
                    logger.info(
                        "pool manager hydrated %d provider capacity quarantines",
                        loaded_groups,
                    )
            except Exception:
                logger.debug(
                    "provider capacity quarantine hydration unavailable",
                    exc_info=True,
                )
        # Build model lists from pool configs
        for name, pool in self.pools.items():
            # Snapshot the operator-curated entries BEFORE any catalog
            # merge so auto_free can distinguish static from auto-added.
            self._original_static[name] = frozenset(
                (m.get("provider", ""), m.get("model", ""))
                for m in pool.models
                if not self._provider_is_disabled(m.get("provider", ""))
            )
            specs = [
                ModelSpec.from_dict(
                    m,
                    default_window=pool.context_window,
                    zdr=pool.zdr,
                    provider_zdr_ok=self._provider_zdr_ok(m.get("provider", "")),
                )
                for m in pool.models
                if not self._provider_is_disabled(m.get("provider", ""))
            ]
            usable, unkeyed = _split_unkeyed(
                specs,
                self.config.get("provider_api_keys", {}),
                self._providers,
            )
            for s, reason in unkeyed:
                logger.warning(
                    "pool '%s': dropping %s '%s' — %s (add the key to the provider secret to enable)",
                    name, s.provider, s.model, reason,
                )
            self.unkeyed[name] = unkeyed
            self.models[name] = usable
            # Warn about unknown providers once at startup
            for w in _validate_providers(specs, self._providers):
                logger.warning("pool '%s': %s — will be skipped during selection", name, w)
    def pool_keeps_heavyweight(self, pool_name: str) -> bool:
        """Return True if the named pool should keep heavyweight candidates.

        Cheap-tier pools (code, privacy) drop heavyweights.
        Premium-tier pools (premium, swarm) keep them.
        """
        return pool_name in PREMIUM_POOLS

    def fallback_pools(self, pool_name: str) -> tuple[str, ...]:
        """Return configured, existing pools to try after ``pool_name``.

        Fallbacks are an explicit pool-level policy so a cheap alias does not
        silently become a paid route. Unknown names and self-references are
        ignored at selection time.
        """
        pool = self.pools.get(pool_name)
        if pool is None:
            return ()
        return tuple(
            fallback
            for fallback in getattr(pool, "fallback_pools", ())
            if fallback in self.pools and fallback != pool_name
        )

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
            if not self._provider_is_disabled(m.get("provider", ""))
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

        OpenRouter and provider-native catalogs contribute models whose input
        and output pricing is explicitly zero. OpenCode Zen/Go catalogs are
        key-filtered, so all advertised models are eligible. Xiaomi's
        authenticated catalog is also key-filtered, but its models are only
        added to non-ZDR cheap pools and pricing/slug-based heavyweights are
        excluded. Operators can opt additional authenticated catalogs into a
        pool with ``auto_catalog_providers``; those providers are still
        subject to the pool's heavyweight, privacy, and tool-capability gates.

        Auto-added entries are tracked separately from operator-curated static
        entries so refreshes can add and prune models without disturbing the
        configured allowlist.
        """
        if self.catalog_registry is None:
            return {}

        changed = False
        excluded_auto_free_providers = {
            str(provider).strip().lower().replace("_", "-")
            for provider in self.config.get("auto_free_excluded_providers", ())
            if str(provider).strip()
        }
        for pool_name, pool in self.pools.items():
            if not pool.auto_free:
                continue

            auto_catalog_providers = {
                str(name).strip().lower().replace("_", "-")
                for name in getattr(pool, "auto_catalog_providers", ())
                if str(name).strip()
            }

            static_pairs = set(self._original_static.get(pool_name, frozenset()))
            eligible: dict[tuple[str, str], dict[str, Any]] = {}
            excluded_special_models: list[str] = []

            providers = getattr(self.catalog_registry, "providers", None)
            catalog_providers = (
                tuple(providers())
                if callable(providers)
                else tuple(getattr(self.catalog_registry, "_clients", {}))
                or ("openrouter", "opencode-zen", "opencode-go", "xiaomi")
            )
            for provider in catalog_providers:
                if provider == "models.dev":
                    # models.dev is a pricing source, not an inference route.
                    continue
                if provider in excluded_auto_free_providers:
                    logger.info(
                        "auto_free pool '%s': excluded provider '%s'",
                        pool_name,
                        provider,
                    )
                    continue
                if self._provider_is_disabled(provider):
                    logger.info(
                        "auto_free pool '%s': disabled provider '%s'",
                        pool_name,
                        provider,
                    )
                    continue
                if pool.zdr and not self._provider_zdr_ok(provider):
                    # A free catalog entry is not automatically privacy-safe.
                    # Keep privacy discovery constrained to providers whose
                    # policy was explicitly reviewed in the registry.
                    continue
                if provider in auto_catalog_providers:
                    # An explicitly opted-in authenticated catalog represents
                    # the models available to this account, not a public
                    # free-price catalogue. Tool-bearing requests still need
                    # a passing behavioral qualification record.
                    mode = "catalog"
                elif provider in {"opencode-zen", "opencode-go"}:
                    mode = "all"
                elif provider == "xiaomi":
                    mode = "xiaomi"
                else:
                    # Generic provider catalogs may only auto-enter a cheap
                    # pool when both prices are explicitly zero.
                    mode = "pricing"
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
                        "auto_discovered": True,
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
                if (
                    not self._provider_is_disabled(model.get("provider", ""))
                    and (model.get("provider", ""), model.get("model", ""))
                    in static_pairs
                )
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
                ModelSpec.from_dict(
                    m,
                    default_window=pool.context_window,
                    zdr=pool.zdr,
                    provider_zdr_ok=self._provider_zdr_ok(m.get("provider", "")),
                )
                for m in pool.models
                if not self._provider_is_disabled(m.get("provider", ""))
            ]
            usable, unkeyed = _split_unkeyed(
                specs,
                self.config.get("provider_api_keys", {}),
                self._providers,
            )
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
        if modalities is None and self._model_capability_db is not None:
            # A catalog refresh records modality claims in the persistent DB
            # as well as retaining the in-memory catalog row. Use those
            # records when a provider's catalog is temporarily unavailable
            # or its row does not carry the modality fields. Explicit pool
            # metadata remains authoritative over this fallback.
            discovered_modalities = {
                record.capability.removeprefix("input_")
                for record in self._model_capability_db.for_model(
                    spec.provider, spec.model
                )
                if record.capability.startswith("input_")
                and record.status in {"advertised", "passed"}
            }
            if discovered_modalities:
                modalities = frozenset(discovered_modalities)
        tool_support = advertised_tool_support(entry)
        capability_cache[key] = (modalities, tool_support)
        return modalities, tool_support

    def _input_modalities_allowed(
        self,
        spec: ModelSpec,
        required_modalities: frozenset[str],
        advertised_modalities: frozenset[str] | None,
    ) -> bool:
        """Apply verified modality evidence before catalog compatibility.

        A passed/unsupported probe is authoritative for that one input
        modality. An advertised catalog claim is used when no explicit probe
        exists. If neither is present for non-text input, the candidate is
        held back instead of turning missing metadata into a provider 400.
        """
        for modality in required_modalities:
            capability = f"input_{modality}"
            record = (
                self._model_capability_db.get(spec.provider, spec.model, capability)
                if self._model_capability_db is not None
                else None
            )
            if record is not None:
                if record.status == "passed":
                    continue
                if record.status == "unsupported":
                    return False
                # advertised/discovered/unavailable/unknown do not override
                # the normal catalog result below.
            if (
                advertised_modalities is not None
                and modality not in advertised_modalities
            ):
                return False
            # Dynamic catalogs and operator-curated pools can both contain
            # models whose capability metadata is missing. Admitting one for
            # image/audio/video input turns an information gap into a
            # provider 400 on the first real request. Text remains fail-open
            # because it is the baseline contract for chat models. An explicit
            # modality probe above can still admit a model whose catalog is
            # incomplete.
            if (
                modality != "text"
                and advertised_modalities is None
                and (record is None or record.status not in {"passed"})
            ):
                return False
        return True

    def _effective_modalities_for_status(
        self,
        spec: ModelSpec,
        capability_cache: dict[
            tuple[str, str], tuple[frozenset[str] | None, bool | None]
        ],
    ) -> tuple[frozenset[str] | None, frozenset[str] | None]:
        """Return current input/output modality evidence for diagnostics.

        Catalog metadata is the baseline. An explicit live probe can add a
        capability that stale metadata omitted, or remove one that the
        catalog incorrectly advertised. ``None`` remains meaningful: it
        means the gateway has no current claim for that direction.
        """
        input_modalities, _ = self._model_capabilities(spec, capability_cache)
        output_modalities = advertised_output_modalities(self._catalog_entry_for(spec))
        records = (
            self._model_capability_db.for_model(spec.provider, spec.model)
            if self._model_capability_db is not None
            else []
        )

        def apply_evidence(
            baseline: frozenset[str] | None,
            prefix: str,
        ) -> frozenset[str] | None:
            effective = set(baseline) if baseline is not None else None
            for record in records:
                if not record.capability.startswith(prefix):
                    continue
                modality = record.capability[len(prefix):]
                if record.status == "passed":
                    if effective is None:
                        effective = set()
                    effective.add(modality)
                elif record.status == "unsupported" and effective is not None:
                    effective.discard(modality)
            return frozenset(effective) if effective is not None else None

        return (
            apply_evidence(input_modalities, "input_"),
            apply_evidence(output_modalities, "output_"),
        )

    def _tool_capability_allowed(
        self,
        spec: ModelSpec,
        *,
        allow_unqualified_static_tools: bool = False,
        allow_structured_tool_fallback: bool = False,
        allow_tool_compatibility_fallback: bool = False,
    ) -> bool:
        """Return whether ``spec`` may receive a tool-bearing request.

        Operator-curated static entries remain compatible with the historical
        fail-open behavior when they have never been probed. Newly discovered
        catalog entries are held back until the qualification runner records a
        passing streaming tool contract. Explicit behavioral failures remain
        excluded, while transient availability results are retried after their
        provider/model cooldown instead of becoming permanent exclusions.
        """
        mode = os.environ.get("TUSKER_TOOL_CAPABILITY_GATE", "auto").strip().lower()
        if mode in {"0", "false", "no", "off", "disabled"}:
            return True
        if self._tool_capabilities is None:
            return not spec.auto_discovered or mode != "strict"
        result = self._tool_capabilities.get(spec.provider, spec.model)
        if result is not None:
            if result.qualified_for_tools:
                return True
            if (
                (
                    allow_unqualified_static_tools
                    and not spec.auto_discovered
                )
                or allow_structured_tool_fallback
            ) and result.level == ToolCapabilityLevel.STRUCTURED_STREAM:
                # The runtime stream boundary validates arguments and removes
                # duplicate/text-form tool markup before committing a response.
                # A model that produced a structured call but failed the
                # probe's stricter no-prose contract is therefore a useful
                # bounded fallback when every strictly qualified route is
                # unavailable. This does not admit unknown or no-call models.
                return True
            if allow_tool_compatibility_fallback and not spec.auto_discovered:
                # A stale qualification record must not turn a curated pool
                # into an empty pool. This is the final bounded fallback: the
                # runtime response validator still rejects malformed calls,
                # unknown tool names, invalid arguments, and required-call
                # responses that contain no call.
                return True
            # Strict mode intentionally requires a fresh passing qualification
            # for every model. Auto mode may retry an operator-curated model
            # after an unavailable probe; catalog-discovered models stay held
            # until they pass so an outage cannot turn into tool-call leakage.
            if result.level == ToolCapabilityLevel.UNAVAILABLE:
                return mode != "strict" and not spec.auto_discovered
            return False
        if mode == "strict":
            return False
        return not spec.auto_discovered

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
        allow_cooldown_probe: bool = False,
        allow_unqualified_static_tools: bool = False,
        allow_structured_tool_fallback: bool = False,
        allow_tool_compatibility_fallback: bool = False,
        allowed_providers: tuple[str, ...] | None = None,
    ) -> tuple[str, str] | None:
        """Select the best (provider, model) from a pool.

        Failed candidates can be excluded for request-level fallback.

        ``heavyweight_ok`` defaults to the pool's tier: cheap-tier pools
        (code, privacy) drop heavyweights, premium-tier pools (premium,
        swarm) keep them. Pass an explicit True/False to override.

        ``required_input_modalities`` excludes models whose known input
        modalities do not cover the request. All models without evidence for
        non-text input are held back until a catalog claim or explicit
        modality probe makes them eligible. Text remains compatible with
        metadata-free legacy candidates.

        ``requires_tools`` applies the same rule to catalog-advertised tool
        support. Models that explicitly lack tools are excluded; models with
        unknown capability metadata remain eligible for compatibility.

        ``allow_cooldown_probe`` is reserved for the bounded request-level
        recovery path. It ignores individual model/provider cooldowns while
        retaining global and shared-capacity quarantines, so a stale transient
        outage cannot make an otherwise configured fallback chain empty.

        ``allow_unqualified_static_tools`` is a recovery-only escape hatch for
        curated models with a structured but non-strict probe result.

        ``allow_structured_tool_fallback`` extends that bounded recovery hatch
        to auto-discovered models that have already emitted a structured call
        during qualification. The response boundary still validates and
        sanitizes every tool stream.

        ``allow_tool_compatibility_fallback`` is the final recovery-only
        escape hatch for curated models whose persisted probe says tools are
        unsupported. It is used only after strict and structured candidates
        are exhausted; auto-discovered models remain gated.

        ``allowed_providers`` applies caller identity patterns before sticky,
        weighted, quality, or recovery selection. An empty tuple denies every
        candidate; ``None`` leaves provider policy unrestricted.
        """
        excluded = excluded or set()
        required_modalities = frozenset(
            _normalise_input_modality(modality)
            for modality in (required_input_modalities or ())
            if _normalise_input_modality(modality)
        )
        specs = self.models.get(pool_name, [])
        if allowed_providers is not None:
            specs = [
                spec
                for spec in specs
                if any(
                    fnmatch.fnmatchcase(spec.provider, pattern)
                    for pattern in allowed_providers
                )
            ]
        if not specs:
            unkeyed_count = len(self.unkeyed.get(pool_name, ()))
            logger.warning(
                "pool '%s' has no usable candidates configured=%d usable=0 unkeyed=%d",
                pool_name,
                unkeyed_count,
                unkeyed_count,
            )
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
                        pool_config = self.pools.get(pool_name)
                        if pool_config and pool_config.zdr and not s.zdr_ok:
                            self._stickiness.pop(key, None)
                            break
                        if context_tokens > 0 and s.context_window < context_tokens:
                            # Context doesn't fit; clear stickiness
                            self._stickiness.pop(key, None)
                            break
                        modalities, tool_support = self._model_capabilities(
                            s, capability_cache
                        )
                        if not self._input_modalities_allowed(
                            s, required_modalities, modalities
                        ):
                            self._stickiness.pop(key, None)
                            break
                        if requires_tools and tool_support is False and not (
                            allow_tool_compatibility_fallback and not s.auto_discovered
                        ):
                            self._stickiness.pop(key, None)
                            break
                        if requires_tools and not self._tool_capability_allowed(
                            s,
                            allow_unqualified_static_tools=allow_unqualified_static_tools,
                            allow_structured_tool_fallback=allow_structured_tool_fallback,
                            allow_tool_compatibility_fallback=allow_tool_compatibility_fallback,
                        ):
                            self._stickiness.pop(key, None)
                            break
                        sticky_cooldown = (
                            self._cooldowns.is_capacity_cooldown(s.provider, s.model)
                            if allow_cooldown_probe
                            else self._cooldowns.is_cooldown(s.provider, s.model)
                        )
                        if sticky_cooldown:
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
        filtered_tool_capability_models: list[str] = []
        filtered_modality_models: list[str] = []
        filtered_cooldown_models: list[str] = []
        filter_counts = {
            "request_excluded": 0,
            "unregistered_provider": 0,
            "special_purpose": 0,
            "context_window": 0,
            "heavyweight": 0,
            "input_modalities": 0,
            "advertised_tools": 0,
            "behavioral_tools": 0,
            "cooldown": 0,
            "zdr_policy": 0,
        }
        filtered_zdr_models: list[str] = []
        for s in specs:
            if (s.provider, s.model) in excluded:
                filter_counts["request_excluded"] += 1
                continue
            # Skip providers that are not in the registry — avoids ProviderError cascade
            if s.provider not in self._providers:
                filter_counts["unregistered_provider"] += 1
                continue
            if not is_general_chat_model(s.provider, s.model):
                filter_counts["special_purpose"] += 1
                filtered_special_models.append(f"{s.provider}/{s.model}")
                continue
            pool_config = self.pools.get(pool_name)
            if pool_config and pool_config.zdr and not s.zdr_ok:
                filter_counts["zdr_policy"] += 1
                if len(filtered_zdr_models) < 12:
                    filtered_zdr_models.append(f"{s.provider}/{s.model}")
                continue
            if context_tokens > 0 and s.context_window < context_tokens:
                filter_counts["context_window"] += 1
                continue
            if not heavyweight_ok and s.heavyweight:
                filter_counts["heavyweight"] += 1
                continue
            modalities, tool_support = self._model_capabilities(s, capability_cache)
            if not self._input_modalities_allowed(
                s, required_modalities, modalities
            ):
                filter_counts["input_modalities"] += 1
                filtered_modality_models.append(f"{s.provider}/{s.model}")
                continue
            if requires_tools and tool_support is False and not (
                allow_tool_compatibility_fallback and not s.auto_discovered
            ):
                filter_counts["advertised_tools"] += 1
                filtered_tool_models.append(f"{s.provider}/{s.model}")
                continue
            if requires_tools and not self._tool_capability_allowed(
                s,
                allow_unqualified_static_tools=allow_unqualified_static_tools,
                allow_structured_tool_fallback=allow_structured_tool_fallback,
                allow_tool_compatibility_fallback=allow_tool_compatibility_fallback,
            ):
                filter_counts["behavioral_tools"] += 1
                filtered_tool_capability_models.append(f"{s.provider}/{s.model}")
                continue
            cooldown_active = (
                self._cooldowns.is_capacity_cooldown(s.provider, s.model)
                if allow_cooldown_probe
                else self._cooldowns.is_cooldown(s.provider, s.model)
            )
            if cooldown_active:
                filter_counts["cooldown"] += 1
                if len(filtered_cooldown_models) < 12:
                    filtered_cooldown_models.append(f"{s.provider}/{s.model}")
                continue
            # ZDR: operator exclusions remain an additional deny-list.
            if pool_config and pool_config.zdr:
                if s.provider in self.config.get("excluded_providers", []):
                    filter_counts["zdr_policy"] += 1
                    if len(filtered_zdr_models) < 12:
                        filtered_zdr_models.append(f"{s.provider}/{s.model}")
                    continue
            candidates.append(s)

        if requires_tools and filtered_tool_models:
            logger.info(
                "pool '%s' capability filter tools=true filtered=%d models=%s",
                pool_name,
                len(filtered_tool_models),
                ",".join(filtered_tool_models[:12]),
            )
        if requires_tools and filtered_tool_capability_models:
            logger.info(
                "pool '%s' behavioral tool-capability filter filtered=%d models=%s",
                pool_name,
                len(filtered_tool_capability_models),
                ",".join(filtered_tool_capability_models[:12]),
            )
        if filtered_special_models:
            logger.info(
                "pool '%s' special-purpose filter filtered=%d models=%s",
                pool_name,
                len(filtered_special_models),
                ",".join(filtered_special_models[:12]),
            )
        if filtered_zdr_models:
            logger.info(
                "pool '%s' privacy policy filter filtered=%d models=%s",
                pool_name,
                len(filtered_zdr_models),
                ",".join(filtered_zdr_models[:12]),
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
            filters = ",".join(
                f"{name}={count}"
                for name, count in filter_counts.items()
                if count
            ) or "none"
            logger.warning(
                "pool '%s' has no eligible candidates configured=%d requires_tools=%s "
                "input_modalities=%s context_tokens=%d filters=%s cooldown_models=%s "
                "cooldown_probe=%s unqualified_static_tools=%s "
                "structured_tool_fallback=%s tool_compatibility_fallback=%s "
                "unkeyed=%d",
                pool_name,
                len(specs),
                requires_tools,
                "+".join(sorted(required_modalities)) or "none",
                context_tokens,
                filters,
                ",".join(filtered_cooldown_models) or "none",
                allow_cooldown_probe,
                allow_unqualified_static_tools,
                allow_structured_tool_fallback,
                allow_tool_compatibility_fallback,
                len(self.unkeyed.get(pool_name, ())),
            )
            return None

        # 3. Rank by quality, then apply weighted selection within top tier
        quality_list = self._quality.rank(
            [(c.provider, c.model) for c in candidates],
        )

        if not quality_list:
            # Fallback: pick first candidate
            result = (candidates[0].provider, candidates[0].model)
            if session_id:
                key = (session_id, pool_name)
                self._stickiness[key] = result
            return result

        # Group candidates by quality tier (all with same score as the top)
        top_score = quality_list[0][2]  # third element is the score
        tier_candidates = [c for c in candidates if next(
            (q[2] for q in quality_list if q[0] == c.provider and q[1] == c.model),
            -1
        ) == top_score]

        # Within the top tier, apply weighted selection.
        # Equal weights should distribute traffic, while retaining a
        # deterministic order that is easy to observe and test.
        if len(tier_candidates) == 1:
            result = (tier_candidates[0].provider, tier_candidates[0].model)
        else:
            weights = [c.weight for c in tier_candidates]
            # Check if all weights are equal (within floating-point tolerance)
            if max(weights) - min(weights) <= 1e-12:
                offset = self._round_robin.get(pool_name, 0) % len(tier_candidates)
                selected = tier_candidates[offset]
                self._round_robin[pool_name] = offset + 1
            else:
                # Varied weights: use weighted random selection
                total_weight = sum(weights)
                cumulative = [sum(weights[:i+1]) / total_weight for i in range(len(weights))]
                rand_val = random.random()
                selected = tier_candidates[0]
                for idx, cum_weight in enumerate(cumulative):
                    if rand_val <= cum_weight:
                        selected = tier_candidates[idx]
                        break
            
            result = (selected.provider, selected.model)

        if session_id:
            key = (session_id, pool_name)
            self._stickiness[key] = result
        return result

    def status(self) -> dict[str, Any]:
        """Return pool status for /status endpoint."""
        result = {}
        capability_cache: dict[
            tuple[str, str], tuple[frozenset[str] | None, bool | None]
        ] = {}
        for name, specs in self.models.items():
            valid = [s for s in specs if s.provider in self._providers]
            invalid = [s for s in specs if s.provider not in self._providers]
            result[name] = {
                "models": len(specs),
                "valid_candidates": len(valid),
                "auto_catalog_providers": list(
                    getattr(self.pools.get(name), "auto_catalog_providers", ())
                ),
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
                    self._status_candidate(s, capability_cache)
                    for s in valid
                ],
            }
        return result

    def _status_candidate(
        self,
        spec: ModelSpec,
        capability_cache: dict[
            tuple[str, str], tuple[frozenset[str] | None, bool | None]
        ],
    ) -> dict[str, Any]:
        """Serialize one candidate with effective capability evidence."""
        input_modalities, output_modalities = self._effective_modalities_for_status(
            spec, capability_cache
        )
        return {
            "provider": spec.provider,
            "model": spec.model,
            "context_window": spec.context_window,
            "auto_discovered": spec.auto_discovered,
            "weight": spec.weight,
            "input_modalities": (
                sorted(input_modalities) if input_modalities is not None else None
            ),
            "output_modalities": (
                sorted(output_modalities) if output_modalities is not None else None
            ),
            "tool_capability": (
                self._tool_capabilities.get(spec.provider, spec.model).to_dict()
                if self._tool_capabilities is not None
                and self._tool_capabilities.get(spec.provider, spec.model) is not None
                else None
            ),
            "model_capabilities": (
                [
                    record.to_dict()
                    for record in self._model_capability_db.for_model(
                        spec.provider, spec.model
                    )
                ]
                if self._model_capability_db is not None
                else []
            ),
        }
