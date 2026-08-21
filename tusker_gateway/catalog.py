"""Dynamic catalog refresh for upstream model providers.

Each provider has its own model catalog that changes over time. To track
free-tier model availability (especially OpenRouter `:free` slugs that
come and go frequently) without manually editing TUSKER_POOL_*, the
gateway pulls live catalogs at startup and periodically refreshes them.

Architecture:
    CatalogClient (TTL cache + http GET)
        ├── CodexCatalog    (chatgpt.com/backend-api/codex/models)
        ├── CopilotCatalog  (api.githubcopilot.com/models)
        ├── OpenRouterCatalog (openrouter.ai/api/v1/models)
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
from typing import Any, Iterable

import aiohttp

logger = logging.getLogger(__name__)


# Per-provider TTLs. Mirrors hermes-agent's `_COPILOT_CATALOG_CACHE_TTL=300`
# and the hermes models_dev 1h cache.
DEFAULT_TTLS: dict[str, float] = {
    "openai-codex": 3600.0,        # Codex catalog changes rarely
    "github-copilot": 300.0,       # Copilot: 5 min
    "github-copilot-enterprise": 300.0,
    "openrouter": 3600.0,          # OpenRouter: 60 min
    "models.dev": 3600.0,          # models.dev: 60 min
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
        headers = {
            "User-Agent": "tusker-gateway/1.0 (catalog-refresh)",
            "accept": "application/json",
        }
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
            out.append(CatalogEntry(provider="openrouter", model=slug.strip(), raw=m))
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

    @classmethod
    def default(cls) -> "CatalogRegistry":
        """Build the default registry covering Codex, Copilot, OpenRouter,
        and models.dev."""
        reg = cls()
        reg.register("openai-codex", CodexCatalog())
        reg.register("github-copilot", CopilotCatalog())
        reg.register("github-copilot-enterprise", CopilotCatalog())
        reg.register("openrouter", OpenRouterCatalog())
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
) -> None:
    """Background loop: refresh all catalogs every ``interval_secs``.

    First refresh happens immediately (so the pool has data on first
    request), then every ``interval_secs``. Returns when ``stop_event``
    is set. Exceptions are logged and swallowed so a transient upstream
    failure doesn't kill the loop.
    """
    try:
        await registry.refresh_all(session)
    except Exception as exc:
        logger.warning("initial catalog refresh failed: %s", exc)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_secs)
            return  # stop requested
        except asyncio.TimeoutError:
            pass
        try:
            await registry.refresh_all(session)
        except Exception as exc:
            logger.warning("scheduled catalog refresh failed: %s", exc)
