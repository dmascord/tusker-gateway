"""Provider pools: candidate lists, selection, session stickiness.

A PoolConfig defines which (provider, model) pairs participate in a virtual
role alias (hermes-code, hermes-privacy, hermes-premium, hermes-swarm).
"""
from __future__ import annotations

import logging
import math
import os
import threading
import random
import re
import time
import fnmatch
from dataclasses import dataclass, field
from typing import Any, Callable, MutableMapping

from tusker_gateway.catalog import (
    AUTO_DISCOVERED_HEAVYWEIGHT_WEIGHT,
    advertised_input_modalities,
    advertised_output_modalities,
    advertised_tool_support,
    researched_input_modalities,
)
from tusker_gateway.config import (
    DEFAULT_PROVIDER_REGISTRY,
    PoolConfig,
)
from tusker_gateway.context_limits import declared_context_window
from tusker_gateway.cooldown import CooldownTracker, global_tracker, is_permanently_failed
from tusker_gateway.heavyweight import is_heavyweight
from tusker_gateway.model_rankings import normalize_slug
from tusker_gateway.safety import is_runtime_blacklisted
from tusker_gateway.value import (
    BudgetPacer,
    IntelPrior,
    ProviderPlan,
    RouteCostModel,
    UsageMix,
    load_provider_plans,
    score_candidates,
    value_routing_mode,
)
from tusker_gateway.model_capability import (
    INPUT_MODALITY_CAPABILITIES,
    PRIVACY_ZDR_CAPABILITY,
    PRIVACY_ZDR_PROBE_VERSION,
    STRUCTURED_OUTPUT_CAPABILITY,
    STRUCTURED_OUTPUT_PROBE_VERSION,
)
from tusker_gateway.quality import QualityDB
from tusker_gateway.tool_capability import (
    ToolCapabilityDB,
    ToolCapabilityLevel,
    default_tool_capability_db_path,
)
from tusker_gateway.storage import (
    shared_state_target,
    state_key_revision,
    state_revision,
)

logger = logging.getLogger(__name__)


COMPOSITE_IMAGE_TOOLS_CAPABILITY = "input_image_tools"
COMPOSITE_IMAGE_TOOLS_PROBE_VERSION = "image-tool-contract-v2"
# other status (notably ``unavailable``: 429, 5xx, timeout) says nothing about
# the contract, so it must not be read as a capability denial.
COMPOSITE_IMAGE_TOOLS_DEFINITIVE_FAILURES = frozenset({"unsupported", "failed"})


def _normalise_input_modality(value: Any) -> str:
    """Normalize configured modality names to the catalog/DB spelling."""
    return str(value).strip().lower().replace("-", "_")


def _capability_route_key(provider: str, model: str) -> tuple[str, str]:
    """Return the normalized key used for capability evidence lookups.

    Capability rows are stored with a normalized provider name, so the
    lookup key must be normalized too. Pool configuration is not
    guaranteed to use the same spelling, and an unnormalized lookup would
    silently miss evidence.
    """
    return (
        str(provider or "").strip().lower().replace("_", "-"),
        str(model or "").strip(),
    )


# Pools that are considered "premium" tiers — heavyweight models are
# kept for these (mirrors hermes-agent's `hermes-premium` semantics).
# Cheap-tier pools (`hermes-code`, `hermes-privacy`) drop heavyweights.
PREMIUM_POOLS: frozenset[str] = frozenset({"premium", "swarm"})

# The weight an ordinary pool entry carries. Auto-discovered heavyweights,
# routers and unpriced routes are demoted below it (see
# ``AUTO_DISCOVERED_HEAVYWEIGHT_WEIGHT``) so they are only used after the
# ordinary candidates are exhausted.
_DEFAULT_POOL_WEIGHT: float = 1.0

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

# Auto-routers pick an arbitrary backend per request, so quality, tool and
# modality evidence recorded against them describes whichever model answered.
# They stay available as last-resort fallbacks but must not rank alongside
# concrete models.
_ROUTER_MODEL_SLUGS: frozenset[str] = frozenset({
    "openrouter/free",
    "openrouter/auto",
    "free",
    "auto",
})


def is_router_model(model: str) -> bool:
    """Return whether a catalog slug names a provider-side auto-router."""
    slug = str(model).strip().lower()
    return slug in _ROUTER_MODEL_SLUGS or slug.startswith("kilo-auto/")

_LANGUAGE_RESTRICTED_MODELS: frozenset[tuple[str, str]] = frozenset({
    # allam-2-7b is an Arabic-first model from SDAIA. It serves Arabic
    # prompts well but returns empty content on English prompts (verified
    # live: "Say exactly: OK" -> content=null, finish_reason=stop), so it
    # is not a usable general-chat backend. Kept explicit rather than
    # slug-matched because "allam" is not a modality keyword.
    ("groq", "allam-2-7b"),
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
    # Language-restricted chat models are not usable general backends.
    if (normalized_provider, normalized_model) in _LANGUAGE_RESTRICTED_MODELS:
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
        if modalities is None:
            researched = researched_input_modalities(provider, model)
            if researched is not None:
                modalities = researched
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



def _spec_has_credentials(
    spec: "ModelSpec",
    credential_sizes: dict[str, int] | None,
) -> bool:
    """Return ``True`` if ``spec`` can authenticate given ``credential_sizes``.

    OAuth and Codex providers require at least one credential in the rotator
    to authenticate requests. When ``credential_sizes`` is ``None`` (test
    apps / bare executors without rotators) we cannot prove the provider is
    unkeyed, so we treat the spec as eligible and let the runtime path
    surface the failure at request time.
    """
    if credential_sizes is None:
        return True
    return int(credential_sizes.get(str(spec.provider).strip().lower().replace("_", "-"), 0)) > 0


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
    # Selection runs in worker threads and may be called repeatedly while a
    # request walks its fallback chain. Cache the evidence per route so
    # each candidate change does not scan the shared database again, and so
    # two pools that share a route reuse the same cached evidence.
    _model_capability_records_cache: dict[tuple[str, str], tuple[Any, ...]] = field(
        init=False, default_factory=dict
    )
    _model_capability_records_cached_at: dict[tuple[str, str], float] = field(
        init=False, default_factory=dict
    )
    _model_capability_records_revisions: dict[tuple[str, str], int] = field(
        init=False, default_factory=dict
    )
    _llm_stats_snapshot_cache: dict[str, tuple[str, int | None]] | None = field(
        init=False, default=None
    )
    _llm_stats_snapshot_cached_at: float = field(init=False, default=0.0)
    _llm_stats_snapshot_revision: int = field(init=False, default=-1)
    _tool_capability_records_cache: dict[tuple[str, str], Any] | None = field(
        init=False, default=None
    )
    _tool_capability_records_cached_at: float = field(init=False, default=0.0)
    _tool_capability_records_revision: int = field(init=False, default=-1)
    _status_cache: tuple[tuple[int, ...], float, dict[str, Any]] | None = field(
        init=False, default=None
    )
    _blacklist_config_signature: tuple[str, ...] = field(init=False, default=())
    _blacklist_exact: frozenset[str] = field(init=False, default_factory=frozenset)
    _blacklist_patterns: tuple[str, ...] = field(init=False, default=())
    _model_capability_cache_lock: threading.Lock = field(
        init=False, default_factory=threading.Lock
    )
    _llm_stats: Any | None = None
    _cooldowns: CooldownTracker | None = None
    # Value-routing state: subscription economics, the catalog cost model,
    # the observed token mix, and the budget pacer that decays a plan's
    # value term as its subscription window fills.
    _value_plans: dict[str, ProviderPlan] = field(default_factory=dict)
    _value_intel: IntelPrior = field(default_factory=IntelPrior)
    _value_cost_model: RouteCostModel = field(default_factory=RouteCostModel)
    _value_usage_mix: UsageMix = field(default_factory=lambda: UsageMix(None))
    _value_pacer: BudgetPacer | None = None
    # Optional catalog registry — when set, PoolManager.extend_pools_with_catalog()
    # merges catalog-known models into the allowlist. See catalog.py.
    catalog_registry: object | None = None
    # pool_name -> set of (provider, model) pairs auto-added by
    # extend_pools_with_auto_catalog(). Tracked separately from
    # static allowlist so we can prune auto-added entries when
    # they stop being free (e.g. stealth/ox-alpha goes paid),
    # while keeping operator-curated entries untouched.
    auto_added: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    # pool_name -> set of (provider, model) pairs from the original
    # TUSKER_POOL_* config, snapshotted at startup. Used by
    # extend_pools_with_auto_catalog() to distinguish operator-curated
    # entries (never pruned) from auto-added ones (pruned when they
    # stop being free).
    _original_static: dict[str, frozenset[tuple[str, str]]] = field(default_factory=dict)
    _stickiness: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    _stickiness_expires: dict[tuple[str, str], float] = field(default_factory=dict)
    _round_robin: dict[str, int] = field(default_factory=dict)
    _disabled_providers: frozenset[str] = field(
        init=False, default_factory=frozenset
    )
    # Lock protecting mutable selection state: ``_round_robin``,
    # ``_stickiness``, and ``_stickiness_expires``. ``select()`` is invoked
    # from request handlers via ``asyncio.to_thread``, so concurrent selects
    # can race on these dicts without per-instance serialisation.
    _selection_lock: threading.Lock = field(
        init=False, default_factory=threading.Lock
    )
    STICKINESS_TTL = 3600.0  # 1 hour
    STICKINESS_MAX_ENTRIES = 10_000

    def _drop_stickiness(self, key: tuple[str, str]) -> None:
        self._stickiness.pop(key, None)
        self._stickiness_expires.pop(key, None)

    def _prune_stickiness(self, now: float) -> None:
        for key, expires_at in list(self._stickiness_expires.items()):
            if expires_at <= now:
                self._drop_stickiness(key)
        while len(self._stickiness) >= self.STICKINESS_MAX_ENTRIES:
            self._drop_stickiness(next(iter(self._stickiness)))

    def _remember_stickiness(
        self, key: tuple[str, str], result: tuple[str, str]
    ) -> None:
        now = time.monotonic()
        self._prune_stickiness(now)
        self._stickiness[key] = result
        self._stickiness_expires[key] = now + self.STICKINESS_TTL

    def _provider_zdr_ok(self, provider: str) -> bool:
        """Return the configured privacy/ZDR policy for one provider."""
        endpoint = self._providers.get(provider)
        if endpoint is None:
            return False
        if isinstance(endpoint, dict):
            return bool(endpoint.get("zdr_ok", False))
        return bool(getattr(endpoint, "zdr_ok", False))

    def _provider_kind(self, provider: str) -> str:
        """Return the configured auth kind (e.g. ``"oauth"``) for the provider."""
        endpoint = self._providers.get(provider)
        if endpoint is None:
            return ""
        if isinstance(endpoint, dict):
            return str(endpoint.get("kind", endpoint.get("auth_type", "bearer"))).lower()
        return str(
            getattr(endpoint, "kind", getattr(endpoint, "auth_type", "bearer"))
        ).lower()

    def _provider_is_local(self, provider: str) -> bool:
        """Return whether the provider is a local hardware route.

        Local routes have no per-token price by nature, so the unpriced-entry
        gate must not remove them from a pool.
        """
        return self._provider_kind(provider) == "local"

    def _provider_is_disabled(self, provider: str) -> bool:
        """Return whether operator policy disables this provider for pools."""
        return str(provider).strip().lower().replace("_", "-") in self._disabled_providers

    def _model_is_blacklisted(self, provider: str, model: str) -> bool:
        return self._model_is_blacklisted_fast(provider, model)

    def _llm_stats_excludes(self, model: str) -> bool:
        """Whether the cached LLM Stats snapshot excludes this model."""
        if self._llm_stats is None or self._llm_stats_enforcement == "prefer":
            return False
        row = (self._llm_stats_snapshot() or {}).get(normalize_slug(model))
        return row is not None and row[0] == "excluded"

    # LLM Stats rank boost defaults. Models with better (lower) ranks
    # receive a quality-score bonus so they climb into higher tiers where
    # selection prefers them. Formula:
    #   max(0, cap - per_rank * (rank - 1))
    # Both are overridable via ``llm_stats_rank_boost_cap`` /
    # ``llm_stats_rank_boost_per_rank`` config keys.
    _RANK_BOOST_CAP: float = 25.0
    _RANK_BOOST_PER_RANK: float = 1.0

    def _llm_stats_rank(self, model: str) -> int | None:
        """Return the effective LLM Stats rank from the cached snapshot."""
        if self._llm_stats is None:
            return None
        row = (self._llm_stats_snapshot() or {}).get(normalize_slug(model))
        value = row[1] if row is not None else None
        return value if isinstance(value, int) and value > 0 else None

    def _rank_boost(self, rank: int) -> float:
        """Score bonus for one rank, from the configured cap/decay."""
        cap = self._RANK_BOOST_CAP
        per_rank = self._RANK_BOOST_PER_RANK
        try:
            cap = float(self.config.get("llm_stats_rank_boost_cap", cap))
        except (TypeError, ValueError):
            pass
        try:
            per_rank = float(
                self.config.get("llm_stats_rank_boost_per_rank", per_rank)
            )
        except (TypeError, ValueError):
            pass
        if rank <= 0 or cap <= 0.0 or per_rank <= 0.0:
            return 0.0
        return max(0.0, cap - per_rank * (rank - 1))

    def __post_init__(self):
        self.pools = dict(self.config.get("pools", {}))
        self._providers = _provider_registry(self.config)
        # ``shared_state_target`` raises in PostgreSQL mode without a DSN;
        # the sandboxed default keeps the manager constructible in tests.
        try:
            self._revision_target = shared_state_target(
                self.config["quality_db_path"]
            )
        except Exception:
            self._revision_target = os.fspath(self.config["quality_db_path"])
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
        # Enforcement mode is resolved once per manager: the candidate
        # filter calls the exclusion helper for every candidate on every
        # request, so it must not re-read and re-normalize config strings.
        from tusker_gateway.model_rankings import (
            DEFAULT_ENFORCEMENT,
            ENFORCEMENT_MODES,
            ModelRankingsDB,
        )

        enforcement = str(
            self.config.get("llm_stats_enforcement") or DEFAULT_ENFORCEMENT
        ).strip().lower()
        self._llm_stats_enforcement = (
            enforcement if enforcement in ENFORCEMENT_MODES else DEFAULT_ENFORCEMENT
        )
        llm_stats_path = self.config.get("llm_stats_db_path")
        if llm_stats_path:
            self._llm_stats = ModelRankingsDB(llm_stats_path)
        # Value routing (shadow by default): plan economics from config,
        # list prices from the catalog registry, spend pacing from the
        # provider usage ledger.
        self._value_plans = load_provider_plans(self.config)
        self._value_cost_model = RouteCostModel(
            getattr(self, "catalog_registry", None)
        )
        try:
            from tusker_gateway.provider_usage import (
                ProviderUsageDB,
                default_provider_usage_db_path,
            )

            self._value_usage_db = ProviderUsageDB(
                default_provider_usage_db_path(self.config["quality_db_path"])
            )
            self._value_usage_mix = UsageMix(self._value_usage_db)
            self._value_pacer = BudgetPacer(
                self._value_usage_db,
                self._value_cost_model,
                threshold=float(self.config.get("budget_pacing_threshold", 0.8)),
            )
        except Exception as exc:
            logger.warning("value routing: usage ledger unavailable (%s)", exc)
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
            # merge so auto_catalog can distinguish static from auto-added.
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
            # Pre-seed quality scores for operator-curated models so that
            # known-good static entries always outrank unknown auto-discovered
            # catalog candidates (which start at the adaptive floor ~40).
            # The pre-seed is overwritten by the first real event, so a broken
            # model immediately drops to its true score.
            for s in usable:
                self._quality.prime_model(s.provider, s.model)
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

    def _outbound_model(self, provider: str, model: str) -> str:
        """Resolve the configured model alias used by upstream requests."""
        endpoint = self._providers.get(provider)
        aliases = (
            endpoint.get("model_aliases", {})
            if isinstance(endpoint, dict)
            else getattr(endpoint, "model_aliases", {})
        )
        return aliases.get(model, model)

    def _catalog_unavailable_routes(
        self, specs: list[ModelSpec]
    ) -> set[tuple[str, str]]:
        """Return static routes absent from successful authoritative catalogs."""
        registry = self.catalog_registry
        get_client = getattr(registry, "get_client", None)
        known_models = getattr(registry, "known_models", None)
        if not callable(get_client) or not callable(known_models):
            return set()
        providers = {
            str(provider).strip().lower().replace("_", "-")
            for provider in self.config.get("authoritative_catalog_providers", ())
        }
        unavailable: set[tuple[str, str]] = set()
        for provider in providers.intersection(spec.provider for spec in specs):
            client = get_client(provider)
            if client is None:
                continue
            try:
                diagnostics = client.diagnostics()
                if diagnostics.get("last_refresh_status") != "ok" or diagnostics.get("stale", True):
                    continue
                catalog_models = known_models(provider)
            except Exception:
                continue
            if not catalog_models:
                continue
            unavailable.update(
                (spec.provider, spec.model)
                for spec in specs
                if spec.provider == provider
                and self._outbound_model(provider, spec.model) not in catalog_models
            )
        return unavailable

    def extend_pools_with_catalog(self) -> dict[str, int]:
        """Count catalog-confirmed configured routes, resolving outbound aliases."""
        if self.catalog_registry is None:
            return {}
        confirmed: dict[str, int] = {}
        for pool_name in self.pools:
            count = 0
            for provider, model in self.static_allowlist(pool_name):
                if not provider or not model:
                    continue
                catalog_models = self.catalog_registry.known_models(provider)
                if catalog_models is not None and self._outbound_model(provider, model) in catalog_models:
                    count += 1
            confirmed[pool_name] = count
        self._value_cost_model._cached_at = 0.0
        logger.info("catalog confirmed %s pool entries", confirmed)
        return confirmed
    def extend_pools_with_auto_catalog(self) -> dict[str, list[str]]:
        """Auto-promote eligible catalog models into ``auto_catalog`` pools.
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
        excluded_auto_catalog_providers = {
            str(provider).strip().lower().replace("_", "-")
            for provider in self.config.get("auto_catalog_excluded_providers", ())
            if str(provider).strip()
        }
        for pool_name, pool in self.pools.items():
            if not pool.auto_catalog:
                continue

            auto_catalog_providers = {
                str(name).strip().lower().replace("_", "-")
                for name in getattr(pool, "auto_catalog_providers", ())
                if str(name).strip()
            }

            static_pairs = set(self._original_static.get(pool_name, frozenset()))
            eligible: dict[tuple[str, str], dict[str, Any]] = {}
            excluded_special_models: list[str] = []
            excluded_unpriced_models: list[str] = []

            providers = getattr(self.catalog_registry, "providers", None)
            catalog_providers = (
                tuple(providers())
                if callable(providers)
                else tuple(getattr(self.catalog_registry, "_clients", {}))
                or ("openrouter", "opencode-go", "xiaomi", "kilo")
            )
            for provider in catalog_providers:
                if provider == "models.dev":
                    # models.dev is a pricing source, not an inference route.
                    continue
                if provider in excluded_auto_catalog_providers:
                    logger.info(
                        "auto_catalog pool '%s': excluded provider '%s'",
                        pool_name,
                        provider,
                    )
                    continue
                if self._provider_is_disabled(provider):
                    logger.info(
                        "auto_catalog pool '%s': disabled provider '%s'",
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
                elif provider in {"opencode-go"} and not pool.zdr:
                    mode = "all"
                elif pool.zdr:
                    continue
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
                    advertised_output = advertised_output_modalities(entry)
                    if advertised_output is not None and advertised_output - {"text"}:
                        # Chat pools emit text; a TTS/transcription/image-only
                        # route can only fail here (and poison quality stats).
                        excluded_special_models.append(
                            f"{entry.provider}/{entry.model}"
                        )
                        continue
                    # Skip models that have permanently failed (401/403:
                    # WAF-blocked, agentic-harness-only, wrong-tier). They
                    # would otherwise be re-added and fail on every refresh.
                    # Excluding them here also prunes previously auto-added
                    # dead models (they leave `eligible` → `new_models`).
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
                    if (
                        mode == "catalog"
                        and not self.pool_keeps_heavyweight(pool_name)
                        and not self._provider_is_local(entry.provider)
                        and entry.cost_input is None
                        and entry.cost_output is None
                        and not entry.model.endswith(":free")
                    ):
                        # Unknown pricing is not proof of free, and a cheap
                        # pool must not carry a route whose cost cannot be
                        # established. A 0.05 weight is not enough on its
                        # own: weights only break ties inside the top
                        # selection tier, so an expensive-but-unpriced
                        # route could still win a request outright
                        # (commandcode/Qwen/Qwen4-Max served 9 requests on
                        # 2026-11-20 with a 0.05 weight). Local hardware
                        # routes are exempt: they have no per-token price by
                        # nature, and an explicit ":free" marker is a
                        # published price of zero.
                        excluded_unpriced_models.append(
                            f"{entry.provider}/{entry.model}"
                        )
                        continue
                    if mode == "xiaomi" and heavyweight:
                        continue
                    if pool.heavyweight_only and not heavyweight:
                        # Premium-tier auto-adoption: only heavyweight
                        # entries enter (docs/solution.md: "heavyweight or
                        # premium candidates"). Static entries are
                        # unaffected — they are outside this loop.
                        continue

                    model_data: dict[str, Any] = {
                        "provider": entry.provider,
                        "model": entry.model,
                        "auto_discovered": True,
                    }
                    model_data["heavyweight"] = heavyweight
                    # Deprioritise auto-discovered heavyweights so they are only
                    # selected after all weight-1.0 candidates are exhausted.
                    # This prevents expensive catalog entries (e.g. claude-sonnet-4.6
                    # via copilot) from burning a limited budget alongside the
                    # static models before the quality DB has evidence.
                    if heavyweight or is_router_model(entry.model):
                        model_data["weight"] = AUTO_DISCOVERED_HEAVYWEIGHT_WEIGHT
                    elif (
                        entry.cost_input is None
                        and entry.cost_output is None
                        and not entry.model.endswith(":free")
                    ):
                        # Unknown pricing is not proof of free. Command Code's
                        # catalog omits prices for expensive routes (e.g.
                        # deepseek-v4.1-flash-ultrafast at $2.40/$9.60 per M),
                        # so an unpriced entry must not enter a cheap pool at
                        # full weight; deprioritise until pricing is published.
                        model_data["weight"] = AUTO_DISCOVERED_HEAVYWEIGHT_WEIGHT
                    else:
                        model_data["weight"] = 1.0
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
                    "auto_catalog pool '%s': %d eligible catalog entries",
                    pool_name,
                    len(desired_auto),
                )
            if excluded_special_models:
                logger.info(
                    "auto_catalog pool '%s': excluded %d special-purpose models=%s",
                    pool_name,
                    len(excluded_special_models),
                    ",".join(sorted(excluded_special_models)[:12]),
                )
            if excluded_unpriced_models:
                logger.info(
                    "auto_catalog pool '%s': excluded %d unpriced models=%s",
                    pool_name,
                    len(excluded_unpriced_models),
                    ",".join(sorted(excluded_unpriced_models)[:12]),
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
        model_capability_records: dict[tuple[str, str], tuple[Any, ...]] | None = None,
    ) -> tuple[frozenset[str] | None, bool | None]:
        """Resolve effective input/tool capabilities for a pool candidate."""
        key = _capability_route_key(spec.provider, spec.model)
        cached = capability_cache.get(key)
        if cached is not None:
            return cached
        entry = self._catalog_entry_for(spec)
        modalities = spec.input_modalities
        if modalities is None:
            # Catalog rows are refreshed independently from the persistent
            # capability evidence. A provider may return a sparse or
            # text-only row on a later refresh even though an earlier catalog
            # snapshot (or a live probe) established image/audio/video
            # support. Merge positive evidence instead of letting the newest,
            # incomplete row erase known capabilities. Explicit pool metadata
            # remains authoritative and is intentionally not merged here.
            modalities = advertised_input_modalities(entry)
            if self._model_capability_db is not None:
                records = (
                    model_capability_records.get(key, ())
                    if model_capability_records is not None
                    else self._model_capability_db.for_model(spec.provider, spec.model)
                )
                # ``input_image_tools`` is the composite image-plus-tool
                # contract, not an input modality: it is consumed separately
                # by ``_image_tools_evidence_allows``. Folding it in here
                # added a bogus ``image_tools`` modality and still left the
                # set without ``image``, so an OpenCode Go route whose
                # composite probe passed was rejected by the modality gate.
                evidenced_modalities = {
                    record.capability.removeprefix("input_")
                    for record in records
                    if record.capability.startswith("input_")
                    and record.capability in INPUT_MODALITY_CAPABILITIES
                    and record.status in {"advertised", "discovered", "passed"}
                }
                if evidenced_modalities:
                    modalities = frozenset(
                        set(modalities or ()) | evidenced_modalities
                    )
        tool_support = advertised_tool_support(entry)
        capability_cache[key] = (modalities, tool_support)
        return modalities, tool_support

    def _image_tools_evidence_allows(
        self,
        spec: ModelSpec,
        record: Any | None,
    ) -> bool:
        """Return whether composite image-plus-tool evidence admits this route.

        Image input and tool calling are separate contracts, so a combined
        request needs its own probe. Only a *definitive* verdict is evidence
        about that contract:

        * ``passed`` for the current probe version admits the route;
        * ``unsupported``/``failed`` excludes it, for curated and
          auto-discovered routes alike;
        * ``unavailable`` (429, 5xx, timeout — including the gateway's own
          rate limiter answering a probe burst) and missing/stale evidence
          say nothing about the contract, so they only hold back
          auto-discovered routes that have never proved it. A curated route
          stays eligible, exactly like the behavioural tool gate
          (``_tool_capability_allowed``) treats a transient probe failure.

        Reading a transient probe failure as a capability denial empties a
        pool: on 2026-10-27 a self-rate-limited composite probe cycle
        overwrote passing evidence with ``unavailable`` and every image+tools
        request failed with ``no eligible candidate``
        (rid req_b673132700f2562e, req_b0c6dd248a83c24c,
        req_4e8e6044dfc1d4af).
        """
        if record is not None and (
            record.probe_version == COMPOSITE_IMAGE_TOOLS_PROBE_VERSION
        ):
            status = str(record.status or "").strip().lower()
            if status == "passed":
                return True
            if status in COMPOSITE_IMAGE_TOOLS_DEFINITIVE_FAILURES:
                return False
        return not spec.auto_discovered

    def _privacy_evidence_allows(self, spec: ModelSpec, record: Any | None) -> bool:
        """Return whether privacy-route evidence admits this ZDR candidate.

        A provider marked ``zdr_ok`` is necessary but not sufficient: the
        route must have answered a request that actually carried the pool's
        privacy policy (for Command Code, ``x-cmd-zdr: 1``). Only a
        *definitive* ``privacy_zdr`` probe verdict is evidence about that
        contract:

        * ``passed`` for the current probe version admits the route;
        * ``unsupported``/``failed`` excludes it, curated and
          auto-discovered routes alike;
        * ``unavailable`` (timeout, 5xx, quota) and missing/stale evidence
          say nothing about the contract, so they hold back only
          auto-discovered routes; curated routes stay eligible, matching
          the composite and behavioural tool gates.
        """
        if record is not None and record.probe_version == PRIVACY_ZDR_PROBE_VERSION:
            status = str(record.status or "").strip().lower()
            if status == "passed":
                return True
            if status in COMPOSITE_IMAGE_TOOLS_DEFINITIVE_FAILURES:
                return False
        return not spec.auto_discovered


    def _input_modalities_allowed(
        self,
        spec: ModelSpec,
        required_modalities: frozenset[str],
        advertised_modalities: frozenset[str] | None,
        model_capability_records: dict[tuple[str, str], tuple[Any, ...]] | None = None,
    ) -> bool:
        """Apply verified modality evidence before catalog compatibility.

        A passed/unsupported probe is authoritative for that one input
        modality. An advertised catalog claim is used when no explicit probe
        exists. If neither is present for non-text input, the candidate is
        held back instead of turning missing metadata into a provider 400.
        """
        records = (
            model_capability_records.get(
                _capability_route_key(spec.provider, spec.model), ()
            )
            if model_capability_records is not None
            else None
        )
        for modality in required_modalities:
            capability = f"input_{modality}"
            record = (
                next(
                    (
                        item
                        for item in records
                        if item.capability == capability
                    ),
                    None,
                )
                if records is not None
                else (
                    self._model_capability_db.get(spec.provider, spec.model, capability)
                    if self._model_capability_db is not None
                    else None
                )
            )
            # An explicit pool/catalog declaration is a hard negative. A
            # stale or overly broad successful probe must not turn a known
            # text-only route (notably xiaomi/mimo-v2.5-pro) into a vision
            # candidate.
            if advertised_modalities is not None and modality not in advertised_modalities:
                return False
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

    def _structured_output_status(
        self,
        spec: ModelSpec,
        model_capability_records: dict[tuple[str, str], tuple[Any, ...]] | None,
    ) -> str:
        """Return recent structured-output evidence for one candidate.

        Unknown and transiently unavailable candidates remain eligible so a
        fresh deployment or provider recovery is not made empty by a stale
        probe. A recent explicit unsupported result is the only hard deny;
        when at least one recent pass exists, selection prefers those passes.
        """
        records = (
            model_capability_records.get(
                _capability_route_key(spec.provider, spec.model), ()
            )
            if model_capability_records is not None
            else ()
        )
        record = next(
            (
                item
                for item in records
                if item.capability == STRUCTURED_OUTPUT_CAPABILITY
            ),
            None,
        )
        if record is None or record.probe_version != STRUCTURED_OUTPUT_PROBE_VERSION:
            return "unknown"
        try:
            max_age_secs = max(
                60.0,
                float(os.environ.get(
                    "TUSKER_STRUCTURED_QUALIFICATION_MAX_AGE_SECS",
                    "21600",
                )),
            )
        except (TypeError, ValueError):
            max_age_secs = 21_600.0
        if time.time() - record.checked_at > max_age_secs:
            return "unknown"
        return str(record.status)

    def _effective_modalities_for_status(
        self,
        spec: ModelSpec,
        capability_cache: dict[
            tuple[str, str], tuple[frozenset[str] | None, bool | None]
        ],
        model_capability_records: dict[tuple[str, str], tuple[Any, ...]] | None = None,
    ) -> tuple[frozenset[str] | None, frozenset[str] | None]:
        """Return current input/output modality evidence for diagnostics.

        Catalog metadata is the baseline. An explicit live probe can add a
        capability that stale metadata omitted, or remove one that the
        catalog incorrectly advertised. ``None`` remains meaningful: it
        means the gateway has no current claim for that direction.
        """
        input_modalities, _ = self._model_capabilities(
            spec, capability_cache, model_capability_records
        )
        output_modalities = advertised_output_modalities(self._catalog_entry_for(spec))
        records = (
            model_capability_records.get(
                _capability_route_key(spec.provider, spec.model), ()
            )
            if model_capability_records is not None
            else (
                self._model_capability_db.for_model(spec.provider, spec.model)
                if self._model_capability_db is not None
                else []
            )
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
        capability_cache: dict[tuple[str, str], Any] | None = None,
        require_qualified: bool = False,
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
        if require_qualified:
            if self._tool_capabilities is None:
                return False
            if capability_cache is None:
                result = self._tool_capabilities.get(spec.provider, spec.model)
            else:
                result = capability_cache.get((spec.provider, spec.model))
            if result is None:
                return False
            if result.qualified_for_tools:
                return True
            # A transient probe result (provider 429, 5xx, or transport
            # failure) reports availability, not the tool contract, so it
            # must not be read as a capability denial — the same rule
            # ``_image_tools_evidence_allows`` applies to composite evidence.
            # Reading it as a denial empties a strict pool whose only route
            # was rate-limited at probe time: on 2026-11-01 the privacy pool's
            # ``apim/gpt-5.6-luna`` record held ``rate_limited``, leaving
            # ``mlx-mac/qwen3.8-27b`` as the sole candidate and every
            # image+tools request failed once that route was busy
            # (rid req_ba316b8d545bbbe8). Definitive outcomes (unsupported
            # contract, no tool call, 401/403) still deny, and the runtime
            # stream boundary remains the hard gate on the response.
            return result.transient_for_tools and not spec.auto_discovered

        mode = os.environ.get("TUSKER_TOOL_CAPABILITY_GATE", "auto").strip().lower()
        if mode in {"0", "false", "no", "off", "disabled"}:
            return True
        if self._tool_capabilities is None:
            return not spec.auto_discovered or mode != "strict"
        if capability_cache is None:
            result = self._tool_capabilities.get(spec.provider, spec.model)
        else:
            result = capability_cache.get((spec.provider, spec.model))
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
            # AUTH_FAILED is deliberately excluded from that retry hatch: a
            # 401/403 credential rejection repeats on every retry, so the route
            # is left out of tool-bearing selection until the credential is
            # rotated (and _record_durable_auth_failure has already marked it
            # permanently failed, which drops it from candidate lists too).
            if result.level == ToolCapabilityLevel.UNAVAILABLE:
                return mode != "strict" and not spec.auto_discovered
            return False
        if mode == "strict":
            return False
        return not spec.auto_discovered

    _SELECTION_SNAPSHOT_TTL_SECS = 30.0

    def _llm_stats_snapshot(self) -> dict[str, tuple[str, int | None]] | None:
        """Load LLM Stats verdicts and ranks once per short selection window.

        A sync or seed import must become visible immediately, so the cache
        is invalidated when the store's revision changes.
        """
        if self._llm_stats is None:
            return None
        now = time.monotonic()
        revision = getattr(self._llm_stats, "_revision", 0)
        with self._model_capability_cache_lock:
            if (
                self._llm_stats_snapshot_cache is not None
                and self._llm_stats_snapshot_revision == revision
                and now - self._llm_stats_snapshot_cached_at < self._SELECTION_SNAPSHOT_TTL_SECS
            ):
                return self._llm_stats_snapshot_cache
            snapshot = {
                str(slug): (str(row.get("status", "")), row.get("category_rank"))
                for slug, row in self._llm_stats.rows().items()
            }
            self._llm_stats_snapshot_cache = snapshot
            self._llm_stats_snapshot_cached_at = now
            self._llm_stats_snapshot_revision = revision
            return snapshot

    def _tool_capability_records(self) -> dict[tuple[str, str], Any] | None:
        """Load behavioral qualification rows once per short selection window.

        A new qualification record must become visible immediately, so the
        cache is invalidated when the store's revision changes. The table
        holds one row per probed route and stays small, so a full read is
        cheaper than a query per candidate.
        """
        if self._tool_capabilities is None:
            return None
        now = time.monotonic()
        revision = getattr(self._tool_capabilities, "_revision", 0)
        with self._model_capability_cache_lock:
            if (
                self._tool_capability_records_cache is not None
                and self._tool_capability_records_revision == revision
                and now - self._tool_capability_records_cached_at
                < self._SELECTION_SNAPSHOT_TTL_SECS
            ):
                return self._tool_capability_records_cache
            cached = {
                (record.provider, record.model): record
                for record in self._tool_capabilities.records()
            }
            self._tool_capability_records_cache = cached
            self._tool_capability_records_cached_at = now
            self._tool_capability_records_revision = revision
            return cached

    def _blacklist_matchers(self) -> tuple[frozenset[str], tuple[str, ...]]:
        """Normalize configured blacklist patterns once until config changes."""
        configured = tuple(str(pattern).strip().lower() for pattern in self.config.get("blacklisted_models", ()))
        if configured != self._blacklist_config_signature:
            self._blacklist_config_signature = configured
            self._blacklist_exact = frozenset(
                pattern for pattern in configured if not any(char in pattern for char in "*?[")
            )
            self._blacklist_patterns = tuple(
                pattern for pattern in configured if pattern not in self._blacklist_exact
            )
        return self._blacklist_exact, self._blacklist_patterns

    def _model_is_blacklisted_fast(self, provider: str, model: str) -> bool:
        """Apply static and runtime blacklist policy without repeated setup."""
        normalized_provider = str(provider or "").strip().lower().replace("_", "-")
        normalized_model = str(model or "").strip().lower()
        candidate = f"{normalized_provider}/{normalized_model}"
        if is_runtime_blacklisted(normalized_provider, normalized_model):
            return True
        exact, patterns = self._blacklist_matchers()
        return candidate in exact or any(fnmatch.fnmatchcase(candidate, pattern) for pattern in patterns)
    _MODEL_CAPABILITY_CACHE_TTL_SECS = 30.0

    def _model_capability_records(
        self,
        pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], tuple[Any, ...]] | None:
        """Return capability evidence for the given routes.

        Evidence is cached per route. A route's cached entry is dropped as
        soon as the store reports a write for that route, so a probe
        result becomes visible immediately instead of after the TTL, and
        two pools that share a route reuse the same cached entry.
        """
        if self._model_capability_db is None:
            return None
        now = time.monotonic()
        wanted = list(dict.fromkeys(pairs))
        with self._model_capability_cache_lock:
            stale = [
                key
                for key in wanted
                if key in self._model_capability_records_cache
                and (
                    now - self._model_capability_records_cached_at.get(key, 0.0)
                    >= self._MODEL_CAPABILITY_CACHE_TTL_SECS
                    or self._model_capability_records_revisions.get(key)
                    != self._capability_key_revision(key)
                )
            ]
            for key in stale:
                self._model_capability_records_cache.pop(key, None)
                self._model_capability_records_cached_at.pop(key, None)
                self._model_capability_records_revisions.pop(key, None)
            missing = [
                key
                for key in wanted
                if key not in self._model_capability_records_cache
            ]
            if missing:
                records = self._model_capability_db.records_for_pairs(missing)
                grouped: dict[tuple[str, str], list[Any]] = {}
                for record in records:
                    grouped.setdefault(
                        (record.provider, record.model), []
                    ).append(record)
                for key in missing:
                    self._model_capability_records_cache[key] = tuple(
                        grouped.get(key, ())
                    )
                    self._model_capability_records_cached_at[key] = now
                    self._model_capability_records_revisions[key] = (
                        self._capability_key_revision(key)
                    )
            return {
                key: self._model_capability_records_cache[key]
                for key in wanted
                if key in self._model_capability_records_cache
            }

    def _capability_key_revision(self, key: tuple[str, str]) -> int:
        """Return the store's write revision for one route's evidence."""
        revision_key = getattr(self._model_capability_db, "_revision_key", None)
        if revision_key is None:
            return 0
        return state_key_revision(revision_key[0], revision_key[1], key)

    def select(
        self,
        pool_name: str,
        *,
        context_tokens: int = 0,
        session_id: str | None = None,
        preferred: str | None = None,
        heavyweight_ok: bool | None = None,
        excluded: set[tuple[str, str]] | None = None,
        excluded_providers: frozenset[str] | set[str] | None = None,
        required_input_modalities: set[str] | frozenset[str] | None = None,
        requires_tools: bool = False,
        requires_image_tools: bool = False,
        high_impact_tools: bool = False,
        requires_structured_output: bool = False,
        allow_cooldown_probe: bool = False,
        allow_unqualified_static_tools: bool = False,
        allow_structured_tool_fallback: bool = False,
        allow_tool_compatibility_fallback: bool = False,
        allowed_providers: tuple[str, ...] | None = None,
        allowed_models: tuple[str, ...] | None = None,
        selection_context: str | None = None,
        payload_bytes: int = 0,
        payload_ceiling_of: Callable[[str, str], int | None] | None = None,
        context_failure: MutableMapping[str, int] | None = None,
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

        ``requires_image_tools`` requires a recent passing composite probe when
        an image-bearing request also supplies tools. Unknown and unavailable
        evidence is excluded because image-only and tool-only support do not
        prove the combined request contract.

        ``requires_structured_output`` applies the Hindsight JSON contract.
        Recent failed structured probes are excluded, while a recent passing
        probe is preferred over an unqualified candidate. Unknown candidates
        remain available for recovery and first-use qualification.

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

        ``allowed_models`` applies patterns to the concrete model slug and both
        supported provider-qualified forms. This prevents a permitted virtual
        alias from silently selecting an unpermitted concrete model.
        """
        excluded = excluded or set()
        required_modalities = frozenset(
            _normalise_input_modality(modality)
            for modality in (required_input_modalities or ())
            if _normalise_input_modality(modality)
        )
        pool_config = self.pools.get(pool_name)
        qualification_required = bool(
            pool_config and getattr(pool_config, "require_tool_qualification", False)
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
        if allowed_models is not None:
            specs = [
                spec
                for spec in specs
                if any(
                    fnmatch.fnmatchcase(candidate, pattern)
                    for candidate in (
                        spec.model,
                        f"{spec.provider}/{spec.model}",
                        f"{spec.provider}::{spec.model}",
                    )
                    for pattern in allowed_models
                )
            ]
        if high_impact_tools:
            trusted_patterns = self.config.get("trusted_action_models", ())
            before_action_policy = specs
            specs = [
                spec
                for spec in specs
                if any(
                    fnmatch.fnmatchcase(
                        f"{spec.provider}/{spec.model}", pattern
                    )
                    for pattern in trusted_patterns
                )
            ]
            filtered_action_models = [
                f"{spec.provider}/{spec.model}"
                for spec in before_action_policy
                if spec not in specs
            ]
            if filtered_action_models:
                logger.warning(
                    "high-impact tool model policy filtered=%d models=%s",
                    len(filtered_action_models),
                    ",".join(filtered_action_models[:12]),
                )
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
        model_capability_records = self._model_capability_records(
            [_capability_route_key(spec.provider, spec.model) for spec in specs]
        )
        tool_capability_cache: dict[tuple[str, str], Any] | None = None
        if (requires_tools or qualification_required) and self._tool_capabilities is not None:
            tool_capability_cache = self._tool_capability_records()

        # Image-plus-tool support is a separate contract from ordinary image
        # input and ordinary tool calling. Read its latest probe once per
        # selection so unsupported routes cannot be admitted by two partial
        # successes.
        composite_records = {
            key: next(
                (
                    record for record in records
                    if record.capability == COMPOSITE_IMAGE_TOOLS_CAPABILITY
                ),
                None,
            )
            for key, records in (model_capability_records or {}).items()
        }
        # Privacy-route acceptance is likewise separate evidence: a ZDR
        # provider marking alone does not prove the route answered a
        # request that carried the pool's privacy policy.
        privacy_records = {
            key: next(
                (
                    record for record in records
                    if record.capability == PRIVACY_ZDR_CAPABILITY
                ),
                None,
            )
            for key, records in (model_capability_records or {}).items()
        }


        # Resolve heavyweight gate from pool tier if not explicitly set.
        if heavyweight_ok is None:
            heavyweight_ok = self.pool_keeps_heavyweight(pool_name)
        catalog_unavailable = self._catalog_unavailable_routes(specs)
        # 1. Session stickiness
        if session_id:
            key = (session_id, pool_name)
            self._prune_stickiness(time.monotonic())
            if key in self._stickiness:
                if self._stickiness_expires.get(key, 0) <= time.monotonic():
                    self._drop_stickiness(key)
                else:
                    prev = self._stickiness[key]
            if key in self._stickiness:
                prev = self._stickiness[key]
                for s in specs:
                    if (s.provider, s.model) == prev:
                        if self._model_is_blacklisted(s.provider, s.model):
                            self._drop_stickiness(key)
                            break
                        if self._llm_stats_excludes(s.model):
                            self._drop_stickiness(key)
                            break
                        if (s.provider, s.model) in catalog_unavailable:
                            self._drop_stickiness(key)
                            break
                        if (s.provider, s.model) in excluded:
                            self._drop_stickiness(key)
                            break
                        if not is_general_chat_model(s.provider, s.model):
                            self._drop_stickiness(key)
                            break
                        pool_config = self.pools.get(pool_name)
                        if pool_config and pool_config.zdr and not s.zdr_ok:
                            self._drop_stickiness(key)
                            break
                        _sticky_plan = self._value_plans.get(s.provider)
                        if (
                            pool_config
                            and pool_config.zdr
                            and _sticky_plan is not None
                            and _sticky_plan.trains_on_data
                        ):
                            self._drop_stickiness(key)
                            break
                        declared = (
                            declared_context_window(s.provider, s.model)
                            if context_tokens > 0
                            else None
                        )
                        if declared is not None and declared < context_tokens:
                            # Context doesn't fit; clear stickiness
                            self._drop_stickiness(key)
                            break
                        if payload_bytes > 0 and payload_ceiling_of is not None:
                            sticky_ceiling = payload_ceiling_of(s.provider, s.model)
                            if (
                                sticky_ceiling is not None
                                and sticky_ceiling < payload_bytes
                            ):
                                # This body cannot fit the sticky route; drop
                                # it so the normal filter loop can pick one
                                # that can.
                                self._drop_stickiness(key)
                                break
                        modalities, tool_support = self._model_capabilities(
                            s, capability_cache, model_capability_records
                        )
                        if not self._input_modalities_allowed(
                            s,
                            required_modalities,
                            modalities,
                            model_capability_records,
                        ):
                            self._drop_stickiness(key)
                            break
                        if requires_image_tools and "image" in required_modalities:
                            composite_record = composite_records.get(
                                _capability_route_key(s.provider, s.model)
                            )
                            if not self._image_tools_evidence_allows(
                                s, composite_record
                            ):
                                self._drop_stickiness(key)
                                break
                        if pool_config and pool_config.zdr:
                            if not self._privacy_evidence_allows(
                                s,
                                privacy_records.get(
                                    _capability_route_key(s.provider, s.model)
                                ),
                            ):
                                self._drop_stickiness(key)
                                break
                        if (
                            requires_structured_output
                            and self._structured_output_status(
                                s, model_capability_records
                            ) == "unsupported"
                        ):
                            self._drop_stickiness(key)
                            break
                        if requires_tools and tool_support is False and not (
                            allow_tool_compatibility_fallback and not s.auto_discovered
                        ):
                            self._drop_stickiness(key)
                            break
                        if (requires_tools or qualification_required) and not self._tool_capability_allowed(
                            s,
                            capability_cache=tool_capability_cache,
                            require_qualified=qualification_required,
                            allow_unqualified_static_tools=allow_unqualified_static_tools,
                            allow_structured_tool_fallback=allow_structured_tool_fallback,
                            allow_tool_compatibility_fallback=allow_tool_compatibility_fallback,
                        ):
                            self._drop_stickiness(key)
                            break
                        sticky_cooldown = (
                            self._cooldowns.is_capacity_cooldown(s.provider, s.model)
                            if allow_cooldown_probe
                            else self._cooldowns.is_cooldown(s.provider, s.model)
                        )
                        if sticky_cooldown or is_permanently_failed(s.provider, s.model):
                            self._drop_stickiness(key)
                            break
                        # Don't return a sticky heavyweight if the pool
                        # no longer allows it (config changed mid-session).
                        if not heavyweight_ok and s.heavyweight:
                            self._drop_stickiness(key)
                            break
                        return prev
        # 2. Filter candidates by context window, cooldown, and request-level exclusions
        candidates: list[ModelSpec] = []
        filtered_special_models: list[str] = []
        filtered_tool_models: list[str] = []
        filtered_tool_capability_models: list[str] = []
        filtered_structured_models: list[str] = []
        filtered_modality_models: list[str] = []
        filtered_composite_models: list[str] = []
        filtered_cooldown_models: list[str] = []
        filtered_rank_models: list[str] = []
        filter_counts = {
            "request_excluded": 0,
            "unregistered_provider": 0,
            "blacklisted_model": 0,
            "high_impact_model_policy": 0,
            "catalog_unavailable": 0,
            "special_purpose": 0,
            "context_window": 0,
            "heavyweight": 0,
            "input_modalities": 0,
            "advertised_tools": 0,
            "behavioral_tools": 0,
            "structured_output": 0,
            "image_tools": 0,
            "privacy_evidence": 0,
            "cooldown": 0,
            "zdr_policy": 0,
            "llm_stats_rank": 0,
            "payload_size": 0,
        }
        filtered_zdr_models: list[str] = []
        filtered_context_models: list[str] = []
        context_only_windows: list[int] = []
        filtered_payload_models: list[str] = []
        for s in specs:
            if self._model_is_blacklisted_fast(s.provider, s.model):
                filter_counts["blacklisted_model"] += 1
                continue
            if self._llm_stats_excludes(s.model):
                filter_counts["llm_stats_rank"] += 1
                if len(filtered_rank_models) < 12:
                    filtered_rank_models.append(f"{s.provider}/{s.model}")
                continue
            if (s.provider, s.model) in excluded:
                filter_counts["request_excluded"] += 1
                continue
            # A provider that rate-limited this request is quarantined for the
            # rest of the failover chain. 429s are provider-wide quota signals
            # (the gateway applies provider-level cooldowns for them); without
            # this, a provider with many cheap top-ranked routes can consume
            # every attempt while healthy providers are never reached.
            if excluded_providers and s.provider in excluded_providers:
                filter_counts["request_excluded"] += 1
                continue
            # Skip providers that are not in the registry — avoids ProviderError cascade
            if s.provider not in self._providers:
                filter_counts["unregistered_provider"] += 1
                continue
            if (s.provider, s.model) in catalog_unavailable:
                filter_counts["catalog_unavailable"] += 1
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
            if pool_config and pool_config.zdr and not self._privacy_evidence_allows(
                s,
                privacy_records.get(_capability_route_key(s.provider, s.model)),
            ):
                filter_counts["privacy_evidence"] += 1
                if len(filtered_zdr_models) < 12:
                    filtered_zdr_models.append(f"{s.provider}/{s.model}:privacy_evidence")
                continue
            # Privacy: a provider that trains on request data is hard-masked
            # from ZDR pools even when its registry entry sets zdr_ok — the
            # score can never soften this (see tusker_gateway/value.py).
            if pool_config and pool_config.zdr:
                _plan = self._value_plans.get(s.provider)
                if _plan is not None and _plan.trains_on_data:
                    filter_counts["zdr_policy"] += 1
                    if len(filtered_zdr_models) < 12:
                        filtered_zdr_models.append(
                            f"{s.provider}/{s.model}:trains_on_data"
                        )
                    continue
            # Learned payload ceiling: skip a route we have positive
            # evidence cannot accept this body. ``None`` (no evidence yet)
            # never filters, so a fresh route is always tried.
            if payload_bytes > 0 and payload_ceiling_of is not None:
                ceiling = payload_ceiling_of(s.provider, s.model)
                if ceiling is not None and ceiling < payload_bytes:
                    filter_counts["payload_size"] += 1
                    if len(filtered_payload_models) < 12:
                        filtered_payload_models.append(
                            f"{s.provider}/{s.model}:{ceiling}"
                        )
                    continue
            if not heavyweight_ok and s.heavyweight:
                filter_counts["heavyweight"] += 1
                continue
            modalities, tool_support = self._model_capabilities(
                s, capability_cache, model_capability_records
            )
            if not self._input_modalities_allowed(
                s,
                required_modalities,
                modalities,
                model_capability_records,
            ):
                filter_counts["input_modalities"] += 1
                filtered_modality_models.append(f"{s.provider}/{s.model}")
                continue
            if requires_image_tools and "image" in required_modalities:
                composite_record = composite_records.get(
                    _capability_route_key(s.provider, s.model)
                )
                if not self._image_tools_evidence_allows(s, composite_record):
                    filter_counts["image_tools"] += 1
                    filtered_composite_models.append(f"{s.provider}/{s.model}")
                    continue
            if requires_tools and tool_support is False and not (
                allow_tool_compatibility_fallback and not s.auto_discovered
            ):
                filter_counts["advertised_tools"] += 1
                filtered_tool_models.append(f"{s.provider}/{s.model}")
                continue
            if (requires_tools or qualification_required) and not self._tool_capability_allowed(
                s,
                capability_cache=tool_capability_cache,
                require_qualified=qualification_required,
                allow_unqualified_static_tools=allow_unqualified_static_tools,
                allow_structured_tool_fallback=allow_structured_tool_fallback,
                allow_tool_compatibility_fallback=allow_tool_compatibility_fallback,
            ):
                filter_counts["behavioral_tools"] += 1
                filtered_tool_capability_models.append(f"{s.provider}/{s.model}")
                continue
            if (
                requires_structured_output
                and self._structured_output_status(
                    s, model_capability_records
                ) == "unsupported"
            ):
                filter_counts["structured_output"] += 1
                filtered_structured_models.append(f"{s.provider}/{s.model}")
                continue
            cooldown_active = (
                self._cooldowns.is_capacity_cooldown(s.provider, s.model)
                if allow_cooldown_probe
                else self._cooldowns.is_cooldown(s.provider, s.model)
            )
            if cooldown_active or is_permanently_failed(s.provider, s.model):
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
            # Apply context after every other route eligibility check. This
            # lets callers distinguish a genuinely-too-large request from a
            # pool that is empty for privacy, capability, cooldown, or policy
            # reasons. The pool-wide window is only a default; route-specific
            # declarations preserve long-context candidates.
            window = declared_context_window(s.provider, s.model)
            if context_tokens > 0 and window is not None and window < context_tokens:
                filter_counts["context_window"] += 1
                context_only_windows.append(window)
                if len(filtered_context_models) < 12:
                    filtered_context_models.append(f"{s.provider}/{s.model}:{window}")
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
        if requires_structured_output and filtered_structured_models:
            logger.info(
                "pool '%s' structured-output filter filtered=%d models=%s",
                pool_name,
                len(filtered_structured_models),
                ",".join(filtered_structured_models[:12]),
            )
        if filtered_special_models:
            logger.info(
                "pool '%s' special-purpose filter filtered=%d models=%s",
                pool_name,
                len(filtered_special_models),
                ",".join(filtered_special_models[:12]),
            )
        if filtered_context_models:
            logger.info(
                "pool '%s' context window filter context_tokens=%d filtered=%d models=%s",
                pool_name,
                context_tokens,
                filter_counts["context_window"],
                ",".join(filtered_context_models),
            )
        if filtered_payload_models:
            logger.info(
                "pool '%s' payload ceiling filter payload_bytes=%d filtered=%d models=%s",
                pool_name,
                payload_bytes,
                filter_counts["payload_size"],
                ",".join(filtered_payload_models),
            )
        if filtered_zdr_models:
            logger.info(
                "pool '%s' privacy policy filter filtered=%d models=%s",
                pool_name,
                len(filtered_zdr_models),
                ",".join(filtered_zdr_models[:12]),
            )
        if pool_config and pool_config.zdr and filter_counts["privacy_evidence"]:
            logger.info(
                "pool '%s' privacy evidence filter filtered=%d models=%s",
                pool_name,
                filter_counts["privacy_evidence"],
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
        if requires_image_tools and filtered_composite_models:
            logger.info(
                "pool '%s' composite capability filter input_image_tools filtered=%d models=%s",
                pool_name,
                len(filtered_composite_models),
                ",".join(filtered_composite_models[:12]),
            )

        if filtered_rank_models:
            logger.info(
                "pool '%s' llm-stats capability filter filtered=%d models=%s",
                pool_name,
                len(filtered_rank_models),
                ",".join(filtered_rank_models[:12]),
            )

        if requires_structured_output and candidates:
            qualified_candidates = [
                candidate
                for candidate in candidates
                if self._structured_output_status(
                    candidate, model_capability_records
                ) == "passed"
            ]
            if qualified_candidates:
                candidates = qualified_candidates
                logger.info(
                    "pool '%s' structured-output qualification preferred=%d",
                    pool_name,
                    len(candidates),
                )

        if not candidates:
            if context_failure is not None and context_only_windows:
                context_failure["required_context_tokens"] = int(context_tokens)
                context_failure["max_context_window"] = max(context_only_windows)
            filters = ",".join(
                f"{name}={count}"
                for name, count in filter_counts.items()
                if count
            ) or "none"
            logger.warning(
                "pool '%s' has no eligible candidates context=%s configured=%d "
                "requires_tools=%s input_modalities=%s context_tokens=%d filters=%s "
                "cooldown_models=%s cooldown_probe=%s unqualified_static_tools=%s "
                "structured_tool_fallback=%s tool_compatibility_fallback=%s "
                "structured_output=%s unkeyed=%d",
                pool_name,
                selection_context or "request",
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
                requires_structured_output,
                len(self.unkeyed.get(pool_name, ())),
            )
            return None

        # 3. Rank by quality, then apply weighted selection within top tier.
        # LLM Stats rank adds a score bonus (better rank = higher bonus) so
        # ranked models climb into higher tiers; learned quality still
        # dominates within each rank band.
        quality_list = self._quality.rank(
            [(c.provider, c.model) for c in candidates],
        )
        if self._llm_stats is not None and quality_list:
            # The boost must let ranked models reach the selection tier, not
            # buy a tier win on its own: a rank-28 model with a stale
            # quality of 80 must not outrank a perfect-quality model whose
            # local route never appears in a public leaderboard window.
            # Clamp every boosted score to the best learned quality in the
            # pool so boosts create ties (rotation) instead of wins.
            known_scores = [
                float(score)
                for _, _, score in quality_list
                if score >= 0.0
            ]
            quality_ceiling = max(known_scores) if known_scores else None
            boosted: list[tuple[str, str, float]] = []
            demoted_weight = {
                (c.provider, c.model)
                for c in candidates
                if c.weight < _DEFAULT_POOL_WEIGHT
            }
            for provider, model, score in quality_list:
                if (provider, model) in demoted_weight:
                    # A demoted entry (auto-discovered heavyweight, router,
                    # or unpriced route) carries a reduced weight so it is
                    # only used after the weight-1.0 candidates are
                    # exhausted. An additive rank bonus would lift it into
                    # the top tier and let it win outright, defeating the
                    # demotion — so the boost is skipped for it.
                    boosted.append((provider, model, float(score)))
                    continue
                rank = self._llm_stats_rank(model)
                bonus = (
                    self._rank_boost(rank) if rank is not None else 0.0
                )
                if bonus > 0.0:
                    unboosted = float(score)
                    score = unboosted + bonus
                    # Clamp only models the bonus lifts into the tier: a
                    # below-ceiling model may reach it but never pass it.
                    # Models already at the ceiling keep their boost so a
                    # better rank still orders within-tier ties.
                    if (
                        quality_ceiling is not None
                        and unboosted < quality_ceiling
                    ):
                        score = min(score, quality_ceiling)
                boosted.append((provider, model, score))
            boosted.sort(key=lambda entry: (-entry[2], entry[0], entry[1]))
            quality_list = boosted
        # 2b. Value routing: shadow mode records what the value-adjusted
        # score would pick without changing selection; actual mode replaces
        # the ranking entirely. Never applies on recovery paths (cooldown
        # probe), where the bounded probe sequence owns ordering.
        if candidates and value_routing_mode() != "off" and not allow_cooldown_probe:
            quality_by_pair = {
                (provider, model): float(score)
                for provider, model, score in quality_list
            }
            value_scores = score_candidates(
                pool_name=pool_name,
                candidates=[(c.provider, c.model) for c in candidates],
                quality_scores=quality_by_pair,
                latency_ms={},
                plans=self._value_plans,
                intel=self._value_intel,
                ranks_by_slug={
                    slug: row[1]
                    for slug, row in (self._llm_stats_snapshot() or {}).items()
                },
                cost_model=self._value_cost_model,
                input_fraction=self._value_usage_mix.input_fraction(pool_name),
                pacer=self._value_pacer,
            )
            value_ranked = sorted(
                value_scores, key=lambda s: s.final, reverse=True
            )
            value_pick = (value_ranked[0].provider, value_ranked[0].model)
            if value_routing_mode() == "actual":
                quality_list = [
                    (s.provider, s.model, s.final) for s in value_ranked
                ]
            else:
                actual_pick = (quality_list[0][0], quality_list[0][1])
                if value_pick != actual_pick:
                    from tusker_gateway.value import record_shadow_comparison

                    record_shadow_comparison(pool_name, actual_pick, value_pick)
        if not quality_list:
            # Fallback: pick first candidate
            result = (candidates[0].provider, candidates[0].model)
            if session_id:
                key = (session_id, pool_name)
                self._remember_stickiness(key, result)
            return result

        # Group candidates by quality tier (all with same score as the top)
        top_score = quality_list[0][2]  # third element is the score
        score_by_candidate = {
            (provider, model): score
            for provider, model, score in quality_list
        }
        tier_candidates = [
            c for c in candidates
            if score_by_candidate.get((c.provider, c.model), -1) == top_score
        ]

        # Within the top tier, apply weighted selection.
        # Equal weights should distribute traffic, while retaining a
        # deterministic order that is easy to observe and test.
        if len(tier_candidates) == 1:
            result = (tier_candidates[0].provider, tier_candidates[0].model)
        else:
            weights = [c.weight for c in tier_candidates]
            # Check if all weights are equal (within floating-point tolerance)
            if max(weights) - min(weights) <= 1e-12:
                # Read/advance the round-robin counter under the selection
                # lock so concurrent ``select()`` calls (via
                # ``asyncio.to_thread``) cannot read the same offset and pick
                # the same candidate.
                with self._selection_lock:
                    offset = self._round_robin.get(pool_name, 0) % len(tier_candidates)
                    self._round_robin[pool_name] = offset + 1
                selected = tier_candidates[offset]
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
            with self._selection_lock:
                self._remember_stickiness(key, result)
        return result

    _STATUS_CACHE_TTL_SECS = 10.0

    def status(self) -> dict[str, Any]:
        """Return pool status for /status endpoint.

        The payload walks every pool's candidates and embeds their probe
        records, and callers poll it. A short cache keyed by the revision
        of the underlying evidence stores keeps repeated polls cheap while
        still reflecting a fresh probe or cooldown change within seconds.
        """
        now = time.monotonic()
        revision = (
            state_revision(*self._quality._revision_key),
            state_revision(*self._model_capability_db._revision_key),
            state_revision(*self._tool_capabilities._revision_key),
            (
                state_revision(*self._llm_stats._revision_key)
                if getattr(self._llm_stats, "_revision_key", None)
                else 0
            ),
        )
        cached = self._status_cache
        if cached is not None and cached[0] == revision and now - cached[1] < self._STATUS_CACHE_TTL_SECS:
            return cached[2]
        result = {}
        capability_cache: dict[
            tuple[str, str], tuple[frozenset[str] | None, bool | None]
        ] = {}
        model_capability_records = self._model_capability_records(
            [
                _capability_route_key(spec.provider, spec.model)
                for specs in self.models.values()
                for spec in specs
            ]
        )
        tool_capability_records = self._tool_capability_records()
        for name, specs in self.models.items():
            catalog_unavailable = self._catalog_unavailable_routes(specs)
            valid = [
                s for s in specs
                if s.provider in self._providers
                and (s.provider, s.model) not in catalog_unavailable
                and not is_permanently_failed(s.provider, s.model)
            ]
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
                "catalog_unavailable_entries": [
                    {
                        "provider": provider,
                        "model": model,
                        "reason": "absent_from_authoritative_catalog",
                    }
                    for provider, model in sorted(catalog_unavailable)
                ],
                "unkeyed_entries": [
                    {"provider": s.provider, "model": s.model, "reason": reason}
                    for s, reason in self.unkeyed.get(name, [])
                ],
                "candidates": [
                    self._status_candidate(
                        s,
                        capability_cache,
                        model_capability_records,
                        tool_capability_records,
                    )
                    for s in valid
                ],
            }
        self._status_cache = (revision, now, result)
        return result

    def readiness_status(
        self,
        credential_sizes: dict[str, int] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Report durable request-time eligibility without transient cooldowns.

        ``credential_sizes`` maps provider name -> number of credentials available
        (e.g. OAuth/Codex token rotator size). OAuth- and Codex-kind providers
        without credentials cannot authenticate, so those specs are filtered out
        of the ``selectable`` count even when the provider registry knows them.
        """
        health: dict[str, Any] = {}
        empty: list[str] = []
        for name, specs in self.models.items():
            pool = self.pools.get(name)
            catalog_unavailable = self._catalog_unavailable_routes(specs)
            tool_capability_cache = None
            requires_qualification = bool(
                pool is not None
                and getattr(pool, "require_tool_qualification", False)
            )
            if requires_qualification:
                tool_capability_cache = self._tool_capability_records() or {}
            eligible = [
                spec
                for spec in specs
                if spec.provider in self._providers
                and (spec.provider, spec.model) not in catalog_unavailable
                and not is_permanently_failed(spec.provider, spec.model)
                and is_general_chat_model(spec.provider, spec.model)
                and (pool is None or not pool.zdr or spec.zdr_ok)
                and (self.pool_keeps_heavyweight(name) or not spec.heavyweight)
                and (
                    self._provider_kind(spec.provider) not in {"oauth", "codex"}
                    or _spec_has_credentials(spec, credential_sizes)
                )
                and (
                    not requires_qualification
                    or self._tool_capability_allowed(
                        spec,
                        capability_cache=tool_capability_cache,
                        require_qualified=True,
                    )
                )
            ]
            health[name] = {
                "configured": len(specs) + len(self.unkeyed.get(name, ())),
                "selectable": len(eligible),
                "unkeyed": len(self.unkeyed.get(name, ())),
            }
            if not eligible:
                empty.append(name)
        return health, empty

    def _status_candidate(
        self,
        spec: ModelSpec,
        capability_cache: dict[
            tuple[str, str], tuple[frozenset[str] | None, bool | None]
        ],
        model_capability_records: dict[tuple[str, str], tuple[Any, ...]] | None = None,
        tool_capability_records: dict[tuple[str, str], Any] | None = None,
    ) -> dict[str, Any]:
        """Serialize one candidate with effective capability evidence."""
        input_modalities, output_modalities = self._effective_modalities_for_status(
            spec, capability_cache, model_capability_records
        )
        tool_record = (
            tool_capability_records.get((spec.provider, spec.model))
            if tool_capability_records is not None
            else (
                self._tool_capabilities.get(spec.provider, spec.model)
                if self._tool_capabilities is not None
                else None
            )
        )
        capability_records = (
            model_capability_records.get(
                _capability_route_key(spec.provider, spec.model), ()
            )
            if model_capability_records is not None
            else (
                self._model_capability_db.for_model(spec.provider, spec.model)
                if self._model_capability_db is not None
                else []
            )
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
                tool_record.to_dict() if tool_record is not None else None
            ),
            "model_capabilities": [
                record.to_dict() for record in capability_records
            ],
        }
