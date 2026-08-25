"""Dynamic catalog refresh for upstream model providers.

Each provider has its own model catalog that changes over time. To track
free-tier model availability (especially OpenRouter `:free` slugs that
come and go frequently) without manually editing TUSKER_POOL_*, the
gateway pulls live catalogs at startup and periodically refreshes them.

Architecture:
    CatalogClient (TTL cache + http GET)
        ├── CodexCatalog      (chatgpt.com/backend-api/codex/models)
        ├── CopilotCatalog    (api.githubcopilot.com/models)
        ├── OpenRouterCatalog (openrouter.ai/api/v1/models)
        ├── XiaomiCatalog     (token-plan-sgp.xiaomimimo.com/v1/models)
        └── ModelsDevCatalog  (models.dev/api.json, pricing DB)

    CatalogRegistry — orchestrates refresh, exposes a single
        `catalog_for(provider)` API. Falls back to last-known cached
        state on refresh failure so a transient upstream error doesn't
        empty the pool.

    refresh_task — aiohttp background task that wakes every
        CATALOG_REFRESH_INTERVAL_SECS, refreshes all catalogs, and
        applies the new state to the running PoolManager. Started in
        app.on_startup; cancelled in on_cleanup.

Pool integration:
    PoolManager merges the catalog entries with the static allowlist at
    request time. The catalog extends the pool; static entries are
    always kept regardless of catalog state. Slugs in the catalog that
    are NOT in the static allowlist are *not* surfaced — operators must
    opt in by adding the model to TUSKER_POOL_*.

The pool merging itself lives in pools.py (extend_pool_with_catalog).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import aiohttp

logger = logging.getLogger(__name__)


# Per-provider TTLs. Mirrors hermes-agent's `_COPILOT_CATALOG_CACHE_TTL=300`
# and the hermes models_dev 1h cache.
DEFAULT_TTLS: dict[str, float] = {
    "openai-codex": 3600.0,        # Codex catalog changes rarely
    "github-copilot": 300.0,       # Copilot: 5 min
    "github-copilot-enterprise": 300.0,
    "openrouter": 3600.0,          # OpenRouter: 60 min
    "opencode-zen": 3600.0,       # OpenCode: 60 min (key-filtered)
    "opencode-go": 3600.0,
    "models.dev": 3600.0,          # models.dev: 60 min
    "xiaomi": 3600.0,             # Xiaomi Token Plan: 60 min
}


@dataclass
class CatalogEntry:
    """A single model advertised by an upstream catalog."""

    provider: str               # tusker-gateway provider key
    model: str                  # model slug (catalog-side naming)
    raw: dict[str, Any] = field(default_factory=dict)
    # Optional pricing (filled by models.dev lookup)
    cost_input: float | None = None
    cost_output: float | None = None
    # Known input modalities. None means the catalog does not advertise them.
    input_modalities: frozenset[str] | None = None


@dataclass
class Catalog:
    """A snapshot of one provider's catalog."""

    provider: str
    fetched_at: float
    entries: list[CatalogEntry]
    error: str | None = None  # Set if the last refresh failed (cached state retained)


class CatalogError(Exception):
    """Raised when a catalog fetch fails."""


class CatalogClient:
    """Base class for catalog clients with TTL cache.

    Subclasses implement ``fetch(session)`` returning ``list[CatalogEntry]``.
    The base class handles the time-based cache invalidation; ``get()``
    returns the cached entries until TTL expires, then re-fetches.
    """

    provider: str = ""  # Subclasses set this to the tusker-gateway provider key
    ttl_secs: float = 3600.0  # Subclasses can override

    def __init__(self) -> None:
        self._entries: list[CatalogEntry] = []
        self._fetched_at: float = 0.0
        self._last_error: str | None = None
        self._lock = asyncio.Lock()
        # Subclasses can set ``api_key`` or ``api_key_env`` to inject
        # an Authorization: Bearer header into fetch requests. Set
        # explicitly via ``client.set_api_key(...)`` when wiring up
        # a registry — env-var lookup is the caller's responsibility.
        self._api_key: str | None = None

    def set_api_key(self, api_key: str | None) -> None:
        """Inject an API key for catalog fetches. Pass None to clear."""
        self._api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None

    def _auth_headers(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Return headers dict including Authorization if an API key is set.
        Subclasses pass their default UA/accept dict; the Authorization
        header is layered on top when ``_api_key`` is configured.
        """
        h = dict(base) if base else {}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    async def get(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        """Return cached entries, refreshing if expired."""
        if self._is_stale() or not self._entries:
            await self.refresh(session)
        return self._entries

    async def get_snapshot(self, session: aiohttp.ClientSession) -> Catalog:
        """Return a Catalog snapshot with fetched_at + entries + last error."""
        if self._is_stale() or not self._entries:
            await self.refresh(session)
        return Catalog(
            provider=self.provider,
            fetched_at=self._fetched_at,
            entries=list(self._entries),
            error=self._last_error,
        )

    def is_stale(self) -> bool:
        return self._is_stale()

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) > self.ttl_secs

    async def refresh(self, session: aiohttp.ClientSession) -> None:
        """Re-fetch and replace cached entries. On error, retain last good state."""
        async with self._lock:
            try:
                entries = await self.fetch(session)
                self._entries = entries
                self._fetched_at = time.monotonic()
                self._last_error = None
                logger.info(
                    "%s catalog refreshed: %d entries", self.provider, len(entries)
                )
            except Exception as exc:
                # Retain last good state; just stamp the error.
                self._last_error = str(exc)
                logger.warning(
                    "%s catalog refresh failed (keeping %d cached entries): %s",
                    self.provider, len(self._entries), exc,
                )

    async def fetch(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        """Subclasses override. Return the fresh catalog."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


class CodexCatalog(CatalogClient):
    """Codex catalog from chatgpt.com/backend-api/codex/models."""

    provider = "openai-codex"
    ttl_secs = DEFAULT_TTLS["openai-codex"]

    ENDPOINT = "https://chatgpt.com/backend-api/codex/models?client_version=0.0.0"

    async def fetch(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        headers = {
            "User-Agent": "tusker-gateway/1.0 (catalog-refresh)",
            "accept": "application/json",
        }
        async with session.get(self.ENDPOINT, headers=headers) as resp:
            if resp.status != 200:
                raise CatalogError(f"codex models HTTP {resp.status}")
            data = await resp.json()
        models = data.get("models", []) if isinstance(data, dict) else []
        out: list[CatalogEntry] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            slug = m.get("slug") or m.get("id") or m.get("name")
            if not isinstance(slug, str) or not slug.strip():
                continue
            visibility = (m.get("visibility") or "").strip().lower()
            if visibility in {"hide", "hidden"}:
                continue
            out.append(CatalogEntry(provider="openai-codex", model=slug.strip(), raw=m))
        return out


# ---------------------------------------------------------------------------
# GitHub Copilot
# ---------------------------------------------------------------------------


class CopilotCatalog(CatalogClient):
    """GitHub Copilot catalog from api.githubcopilot.com/models.

    The same endpoint serves github-copilot and github-copilot-enterprise;
    the response format is identical. Both providers get the same catalog.
    """

    provider = "github-copilot"  # Logical: shared with -enterprise
    ttl_secs = DEFAULT_TTLS["github-copilot"]

    ENDPOINT = "https://api.githubcopilot.com/models"

    # The two gateway provider keys we expose the catalog under
    PROVIDER_KEYS = ("github-copilot", "github-copilot-enterprise")

    async def fetch(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        headers = {
            "User-Agent": "tusker-gateway/1.0 (catalog-refresh)",
            "accept": "application/json",
            "Editor-Version": "tusker/1.0",
            "Copilot-Integration-Id": "tusker-gateway",
        }
        async with session.get(self.ENDPOINT, headers=headers) as resp:
            if resp.status != 200:
                raise CatalogError(f"copilot models HTTP {resp.status}")
            data = await resp.json()
        models = data.get("data", []) if isinstance(data, dict) else data
        out: list[CatalogEntry] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            slug = m.get("id") or m.get("name")
            if not isinstance(slug, str) or not slug.strip():
                continue
            slug = slug.strip()
            # One entry per provider key so the registry can serve both.
            for key in self.PROVIDER_KEYS:
                out.append(CatalogEntry(provider=key, model=slug, raw=m))
        return out


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------


class OpenRouterCatalog(CatalogClient):
    """OpenRouter catalog from openrouter.ai/api/v1/models."""

    provider = "openrouter"
    ttl_secs = DEFAULT_TTLS["openrouter"]

    ENDPOINT = "https://openrouter.ai/api/v1/models"

    async def fetch(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        headers = self._auth_headers({
            "User-Agent": "tusker-gateway/1.0 (catalog-refresh)",
            "accept": "application/json",
        })
        async with session.get(self.ENDPOINT, headers=headers) as resp:
            if resp.status != 200:
                raise CatalogError(f"openrouter models HTTP {resp.status}")
            data = await resp.json()
        models = data.get("data", []) if isinstance(data, dict) else data
        out: list[CatalogEntry] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            slug = m.get("id") or m.get("name")
            if not isinstance(slug, str) or not slug.strip():
                continue
            prompt_cost, completion_cost = _extract_openrouter_pricing(m)
            out.append(CatalogEntry(
                provider="openrouter",
                model=slug.strip(),
                raw=m,
                cost_input=prompt_cost,
                cost_output=completion_cost,
            ))
        return out


# ---------------------------------------------------------------------------
class OpenCodeCatalog(CatalogClient):
    """OpenCode Zen / Go catalog from opencode.ai/zen/v1/models and /go/v1/models.

    ``/v1/models`` is key-filtered: the response only contains models
    the configured API key can access. For the gateway's free-tier
    key, that's the full "free for this key" set, and every entry is
    treated as auto-free-eligible (no per-model pricing field).

    Subclasses set ``provider`` ("opencode-zen" or "opencode-go")
    and ``ENDPOINT``. The catalog refresh relies on
    ``set_api_key()`` being called with the matching env-derived
    Bearer token; without auth the endpoint returns a different
    (paid) model list and the auto-free merge would be wrong.
    """

    provider = "opencode-zen"
    ttl_secs = DEFAULT_TTLS["opencode-zen"]

    ENDPOINT = "https://opencode.ai/zen/v1/models"

    async def fetch(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        headers = self._auth_headers({
            "User-Agent": "tusker-gateway/1.0 (catalog-refresh)",
            "accept": "application/json",
        })
        async with session.get(self.ENDPOINT, headers=headers) as resp:
            if resp.status != 200:
                raise CatalogError(f"opencode {self.provider} HTTP {resp.status}")
            data = await resp.json()
        models = data.get("data", []) if isinstance(data, dict) else data
        out: list[CatalogEntry] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            slug = m.get("id") or m.get("name")
            if not isinstance(slug, str) or not slug.strip():
                continue
            out.append(CatalogEntry(
                provider=self.provider,
                model=slug.strip(),
                raw=m,
            ))
        return out


class OpenCodeGoCatalog(OpenCodeCatalog):
    """OpenCode Go (zen/go) backend — same auth/response shape as Zen
    but a different model list and endpoint."""

    provider = "opencode-go"
    ttl_secs = DEFAULT_TTLS["opencode-go"]

    ENDPOINT = "https://opencode.ai/zen/go/v1/models"


# ---------------------------------------------------------------------------
# Xiaomi MiMo Token Plan
# ---------------------------------------------------------------------------


class XiaomiCatalog(CatalogClient):
    """Authenticated Xiaomi Token Plan chat-model catalog.

    Xiaomi's OpenAI-compatible model rows do not carry modality or pricing
    metadata, so the proven chat models are enriched here. Speech-only ASR
    and TTS variants are excluded from chat pool discovery.
    """

    provider = "xiaomi"
    ttl_secs = DEFAULT_TTLS["xiaomi"]

    ENDPOINT = "https://token-plan-sgp.xiaomimimo.com/v1/models"

    # Per-million-token USD pricing and input support verified for the current
    # Token Plan chat models. These values let pool policy apply the same
    # pricing-based heavyweight classification used for other catalogs.
    MODEL_METADATA: dict[str, tuple[frozenset[str], float, float]] = {
        "mimo-v2.5": (frozenset({"text", "image"}), 0.14, 0.28),
        "mimo-v2.5-pro": (frozenset({"text"}), 0.435, 0.87),
    }

    async def fetch(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        headers = self._auth_headers({
            "User-Agent": "tusker-gateway/1.0 (catalog-refresh)",
            "accept": "application/json",
        })
        async with session.get(self.ENDPOINT, headers=headers) as resp:
            if resp.status != 200:
                raise CatalogError(f"xiaomi models HTTP {resp.status}")
            data = await resp.json()

        models = data.get("data", []) if isinstance(data, dict) else data
        out: list[CatalogEntry] = []
        if not isinstance(models, list):
            return out
        for row in models:
            if not isinstance(row, dict):
                continue
            model = row.get("id") or row.get("name")
            if not isinstance(model, str) or not model.strip():
                continue
            model = model.strip()
            normalized = model.lower()
            if normalized.endswith("-asr") or "-tts" in normalized:
                continue
            metadata = self.MODEL_METADATA.get(normalized)
            if metadata is None:
                # The endpoint mixes chat and speech products without a
                # modality field; only emit models proven to be chat-capable.
                continue
            out.append(CatalogEntry(
                provider=self.provider,
                model=model,
                raw=row,
                cost_input=metadata[1],
                cost_output=metadata[2],
                input_modalities=metadata[0],
            ))
        return out


# ---------------------------------------------------------------------------
# models.dev (pricing DB)
# ---------------------------------------------------------------------------


class ModelsDevCatalog(CatalogClient):
    """models.dev pricing DB. Doesn't directly provide models for any
    provider — it provides pricing metadata keyed by ``provider/model``.

    Used to fill in ``cost_input`` / ``cost_output`` on CatalogEntry so
    the heavyweight classifier can do pricing-based detection.
    """

    provider = "models.dev"
    ttl_secs = DEFAULT_TTLS["models.dev"]

    ENDPOINT = "https://models.dev/api.json"

    async def fetch(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        headers = {
            "User-Agent": "tusker-gateway/1.0 (catalog-refresh)",
            "accept": "application/json",
        }
        async with session.get(self.ENDPOINT, headers=headers) as resp:
            if resp.status != 200:
                raise CatalogError(f"models.dev api HTTP {resp.status}")
            data = await resp.json()
        # models.dev schema: { "<provider>": { "models": { "<model>": { ... } } } }
        out: list[CatalogEntry] = []
        if not isinstance(data, dict):
            return out
        for provider, info in data.items():
            if not isinstance(info, dict):
                continue
            models = info.get("models", {})
            if not isinstance(models, dict):
                continue
            for model_name, model_info in models.items():
                if not isinstance(model_info, dict):
                    continue
                cost_input, cost_output = _extract_pricing(model_info)
                out.append(CatalogEntry(
                    provider=str(provider),
                    model=str(model_name),
                    raw=model_info,
                    cost_input=cost_input,
                    cost_output=cost_output,
                ))
        return out


def _extract_pricing(model_info: dict[str, Any]) -> tuple[float | None, float | None]:
    """Extract (cost_input, cost_output) per 1M tokens from a models.dev entry.

    The schema uses nested keys like ``cost.input`` / ``cost.output`` with
    string values that include the unit (e.g. "0.5 / 1M tokens").
    """
    cost = model_info.get("cost")
    if not isinstance(cost, dict):
        return None, None
    return (
        _parse_cost_field(cost.get("input")),
        _parse_cost_field(cost.get("output")),
    )

def _extract_openrouter_pricing(model_info: dict[str, Any]) -> tuple[float | None, float | None]:
    """Extract (cost_input, cost_output) per 1M tokens from an OpenRouter entry.

    OpenRouter schema: ``{"pricing": {"prompt": "0", "completion": "0"}}``
    where the values are per-token dollar strings (e.g. "0.000003" ==
    $3 per 1M tokens). ``"0"`` is the explicit free-tier signal.
    """
    pricing = model_info.get("pricing")
    if not isinstance(pricing, dict):
        return None, None
    return (
        _parse_cost_field(pricing.get("prompt")),
        _parse_cost_field(pricing.get("completion")),
    )

def is_free_openrouter_model(model_info: dict[str, Any]) -> bool:
    """Return whether OpenRouter explicitly prices both token directions at zero."""
    prompt_cost, completion_cost = _extract_openrouter_pricing(model_info)
    return prompt_cost == 0.0 and completion_cost == 0.0

def _parse_cost_field(value: Any) -> float | None:
    """Parse a models.dev cost field like '0.5 / 1M tokens' or 0.5."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        # Strip everything after a slash ("/") and parse the leading number.
        head = value.split("/", 1)[0].strip()
        try:
            return float(head)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Registry — orchestrates all clients
# ---------------------------------------------------------------------------


class CatalogRegistry:
    """Holds one client per provider and exposes catalog snapshots."""

    def __init__(self, clients: dict[str, CatalogClient] | None = None) -> None:
        self._clients: dict[str, CatalogClient] = clients or {}
        self._models_dev: ModelsDevCatalog | None = None
        # Inject models.dev lookup into the other clients via attribute.
        for c in self._clients.values():
            c._pricing_lookup = self._pricing_lookup  # type: ignore[attr-defined]

    def register(self, provider: str, client: CatalogClient) -> None:
        self._clients[provider] = client
        client._pricing_lookup = self._pricing_lookup  # type: ignore[attr-defined]
        if isinstance(client, ModelsDevCatalog):
            self._models_dev = client

    def _pricing_lookup(self, provider: str, model: str) -> tuple[float | None, float | None]:
        """Best-effort pricing lookup from the models.dev catalog cache."""
        if self._models_dev is None:
            return None, None
        for entry in self._models_dev._entries:
            if entry.provider == provider and entry.model == model:
                return entry.cost_input, entry.cost_output
        return None, None

    async def refresh_all(self, session: aiohttp.ClientSession) -> None:
        """Refresh every client concurrently."""
        results = await asyncio.gather(
            *(c.refresh(session) for c in self._clients.values()),
            return_exceptions=True,
        )
        for client, res in zip(self._clients.values(), results):
            if isinstance(res, Exception):
                logger.warning("%s catalog refresh raised: %s", client.provider, res)

    def get_client(self, provider: str) -> CatalogClient | None:
        return self._clients.get(provider)

    async def catalog_for(
        self,
        provider: str,
        session: aiohttp.ClientSession,
    ) -> Catalog | None:
        """Return the catalog snapshot for a provider, or None if no client."""
        client = self._clients.get(provider)
        if client is None:
            return None
        return await client.get_snapshot(session)

    def known_models(self, provider: str) -> set[str] | None:
        """Return set of model slugs known to the catalog for the provider.

        Returns None if no client is registered (catalog doesn't cover this
        provider) so callers can distinguish "unknown provider" from "empty
        catalog".
        """
        client = self._clients.get(provider)
        if client is None:
            return None
        return {e.model for e in client._entries}

    def entries_for(self, provider: str) -> list[CatalogEntry] | None:
        """Return the cached CatalogEntry list for ``provider``.

        Like ``known_models`` but returns the full entries (with
        cost_input/cost_output populated). Returns None when the
        provider has no catalog client, so callers can distinguish
        "unknown provider" from "empty catalog".
        """
        client = self._clients.get(provider)
        if client is None:
            return None
        return list(client._entries)

    @classmethod
    def default(cls) -> "CatalogRegistry":
        """Build the default registry covering Codex, Copilot, OpenRouter,
        OpenCode Zen/Go, Xiaomi, and models.dev."""
        reg = cls()
        reg.register("openai-codex", CodexCatalog())
        reg.register("github-copilot", CopilotCatalog())
        reg.register("github-copilot-enterprise", CopilotCatalog())
        reg.register("openrouter", OpenRouterCatalog())
        reg.register("opencode-zen", OpenCodeCatalog())
        reg.register("opencode-go", OpenCodeGoCatalog())
        reg.register("xiaomi", XiaomiCatalog())
        reg.register("models.dev", ModelsDevCatalog())
        return reg


# ---------------------------------------------------------------------------
# Background refresh task
# ---------------------------------------------------------------------------


async def catalog_refresh_loop(
    registry: CatalogRegistry,
    session: aiohttp.ClientSession,
    interval_secs: float,
    stop_event: asyncio.Event,
    on_refresh: Callable[[], None] | None = None,
) -> None:
    """Background loop: refresh all catalogs every ``interval_secs``.

    First refresh happens immediately (so the pool has data on first
    request), then every ``interval_secs``. Returns when ``stop_event``
    is set. Exceptions are logged and swallowed so a transient upstream
    failure doesn't kill the loop.

    ``on_refresh`` is an optional zero-arg callback invoked after every
    successful refresh_all(); the pool manager uses it to merge
    auto-free catalog entries into the runtime pool, so newly free
    upstreams (e.g. ``stealth/ox-alpha``) enter rotation without a
    gateway restart.
    """
    try:
        await registry.refresh_all(session)
    except Exception as exc:
        logger.warning("initial catalog refresh failed: %s", exc)
    if on_refresh is not None:
        try:
            on_refresh()
        except Exception as exc:
            logger.warning("post-refresh hook failed: %s", exc)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_secs)
            return  # stop requested
        except asyncio.TimeoutError:
            pass
        try:
            await registry.refresh_all(session)
            if on_refresh is not None:
                on_refresh()
        except Exception as exc:
            logger.warning("scheduled catalog refresh failed: %s", exc)
