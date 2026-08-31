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
        ├── ProviderModelsCatalog (provider-native OpenAI-compatible /models)
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
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

import aiohttp

logger = logging.getLogger(__name__)


def _safe_catalog_error_summary(exc: Exception) -> str:
    """Reduce a catalog exception to a credential-safe diagnostic token."""
    match = re.search(r"\bHTTP\s+(\d{3})\b", str(exc), re.IGNORECASE)
    if match:
        return f"http_{match.group(1)}"
    return type(exc).__name__


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
    # Known output modalities. None means the catalog does not advertise them.
    output_modalities: frozenset[str] | None = None


def _capability_values(value: Any) -> frozenset[str] | None:
    """Normalize a catalog capability list without trusting arbitrary values."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    values = {
        str(item).strip().lower().replace("-", "_")
        for item in value
        if isinstance(item, str) and item.strip()
    }
    return frozenset(values)


def _advertised_modalities(
    entry: Any,
    direction: str,
) -> frozenset[str] | None:
    """Return one direction of modalities advertised by a catalog entry.

    OpenRouter puts this in ``architecture.{direction}_modalities`` while a
    few OpenAI-compatible catalogs expose it at the top level. Keep the helper
    permissive so a stale or provider-specific catalog shape leaves the model
    eligible rather than causing a routing failure.
    """
    if direction not in {"input", "output"}:
        raise ValueError(f"unsupported modality direction: {direction}")
    explicit = getattr(entry, f"{direction}_modalities", None)
    if explicit is not None:
        values = _capability_values(explicit)
        if values is not None:
            return values

    raw = getattr(entry, "raw", None)
    if not isinstance(raw, dict):
        return None
    sources = [raw]
    for key in ("architecture", "capabilities"):
        nested = raw.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    field_name = f"{direction}_modalities"
    camel_field_name = f"{direction}Modalities"
    for source in sources:
        for key in (field_name, camel_field_name):
            values = _capability_values(source.get(key))
            if values is not None:
                return values

        # Some catalogs only expose the compact form, e.g. text+image->text.
        modality = source.get("modality")
        if isinstance(modality, str) and "->" in modality:
            modality_part = modality.split("->", 1)[0 if direction == "input" else 1]
            values = frozenset(
                piece.strip().lower()
                for piece in modality_part.replace(",", "+").split("+")
                if piece.strip()
            )
            if values:
                return values
    return None


def advertised_input_modalities(entry: Any) -> frozenset[str] | None:
    """Return input modalities advertised by a catalog entry, if known."""
    return _advertised_modalities(entry, "input")


def advertised_output_modalities(entry: Any) -> frozenset[str] | None:
    """Return output modalities advertised by a catalog entry, if known."""
    return _advertised_modalities(entry, "output")


def advertised_tool_support(entry: Any) -> bool | None:
    """Return whether a catalog entry explicitly advertises tool support.

    ``None`` means unknown. An explicit ``supported_parameters`` list is
    authoritative: a model without ``tools`` in that list must not receive a
    tool-bearing request. This avoids sending requests to models that then
    fail with provider-router errors such as "no endpoints support tool use".
    """
    raw = getattr(entry, "raw", None)
    if not isinstance(raw, dict):
        return None

    sources = [raw]
    for key in ("capabilities", "architecture"):
        nested = raw.get(key)
        if isinstance(nested, dict):
            sources.append(nested)

    boolean_keys = (
        "supports_tools",
        "supports_tool_use",
        "tool_support",
        "tool_use",
        "function_calling",
    )
    parameter_keys = ("supported_parameters", "supportedParameters")
    tool_values = frozenset({
        "tools",
        "functions",
        "function_calling",
        "functioncalling",
        "tool_use",
        "tooluse",
    })

    for source in sources:
        for key in boolean_keys:
            value = source.get(key)
            if isinstance(value, bool):
                return value
        for key in parameter_keys:
            values = _capability_values(source.get(key))
            if values is not None:
                return bool(values & tool_values)
    return None


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
        self._last_refresh_status = "never"
        self._last_refresh_latency_ms: float | None = None
        self._last_refresh_error_class: str | None = None
        self._last_error_summary: str | None = None
        self._last_refresh_at: float | None = None
        self._lock = asyncio.Lock()
        # Subclasses can set ``api_key`` or ``api_key_env`` to inject
        # an Authorization: Bearer header into fetch requests. Set
        # explicitly via ``client.set_api_key(...)`` when wiring up
        # a registry — env-var lookup is the caller's responsibility.
        self._api_key: str | None = None
        self._token_source: Callable[[], Awaitable[str | None]] | None = None

    def set_api_key(self, api_key: str | None) -> None:
        """Inject an API key for catalog fetches. Pass None to clear."""
        self._api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None

    def set_token_source(
        self, source: Callable[[], Awaitable[str | None]] | None
    ) -> None:
        """Inject an async callable returning a per-refresh auth token.

        Used by Codex/Copilot catalogs whose endpoints require a
        short-lived OAuth bearer that the static ``set_api_key`` model
        can't represent. The callable is invoked from
        :meth:`_resolve_token`; a falsy return is treated as
        unauthenticated (the request will fail upstream as 401/400,
        and the cached catalog state is retained). Pass None to clear.
        """
        self._token_source = source

    async def _resolve_token(self) -> str | None:
        """Return the token to use for the next fetch, if any.

        Prefers the dynamic ``_token_source`` (OAuth rotator) and falls
        back to the static ``_api_key`` (long-lived bearer) so
        subclasses don't have to special-case either.
        """
        if self._token_source is not None:
            try:
                tok = await self._token_source()
            except Exception:
                tok = None
            if tok:
                return tok
        return self._api_key

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
            started = time.monotonic()
            try:
                entries = await self.fetch(session)
                self._entries = entries
                self._fetched_at = time.monotonic()
                self._last_error = None
                self._last_refresh_status = "ok"
                self._last_refresh_error_class = None
                self._last_error_summary = None
                self._last_refresh_latency_ms = round(
                    (time.monotonic() - started) * 1000, 1
                )
                self._last_refresh_at = time.time()
                logger.info(
                    "catalog refresh provider=%s status=ok entries=%d "
                    "elapsed_ms=%.1f auth=%s",
                    self.provider,
                    len(entries),
                    self._last_refresh_latency_ms,
                    self.auth_source,
                )
            except Exception as exc:
                # Retain last good state; just stamp the error.
                self._last_error = str(exc)
                self._last_refresh_status = "error"
                self._last_refresh_error_class = type(exc).__name__
                self._last_error_summary = _safe_catalog_error_summary(exc)
                self._last_refresh_latency_ms = round(
                    (time.monotonic() - started) * 1000, 1
                )
                self._last_refresh_at = time.time()
                logger.warning(
                    "catalog refresh provider=%s status=error "
                    "cached_entries=%d elapsed_ms=%.1f auth=%s "
                    "error_class=%s error=%s",
                    self.provider,
                    len(self._entries),
                    self._last_refresh_latency_ms,
                    self.auth_source,
                    self._last_refresh_error_class,
                    exc,
                )

    @property
    def auth_source(self) -> str:
        """Return a non-secret description of the configured auth source."""
        if self._token_source is not None:
            return "oauth"
        if self._api_key:
            return "api_key"
        return "none"

    def diagnostics(self) -> dict[str, Any]:
        """Return credential-safe refresh state for the authenticated status API."""
        endpoint = getattr(self, "endpoint", None) or getattr(self, "ENDPOINT", None)
        return {
            "provider": self.provider,
            "endpoint": str(endpoint) if endpoint else None,
            "auth_source": self.auth_source,
            "entries": len(self._entries),
            "stale": self._is_stale(),
            "last_refresh_status": self._last_refresh_status,
            "last_refresh_at": self._last_refresh_at,
            "last_refresh_latency_ms": self._last_refresh_latency_ms,
            "last_error": self._last_error_summary,
            "last_error_class": self._last_refresh_error_class,
        }

class CodexCatalog(CatalogClient):
    """Codex catalog from chatgpt.com/backend-api/codex/models."""

    provider = "openai-codex"
    ttl_secs = DEFAULT_TTLS["openai-codex"]

    ENDPOINT = "https://chatgpt.com/backend-api/codex/models?client_version=0.0.0"

    async def fetch(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        # The Codex catalog requires a valid OAuth bearer. The token is
        # sourced from the same rotator the Codex chat path uses (see
        # codex_auth_headers / CodexAuthenticator); the Cloudflare bypass
        # header set is identical so the two paths can't drift.
        from tusker_gateway.auth_strategies import codex_auth_headers
        token = await self._resolve_token()
        if token:
            headers = codex_auth_headers(token)
            headers["accept"] = "application/json"
        else:
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
    """GitHub Copilot catalog from a Copilot models endpoint.

    Public Copilot lives at ``https://api.githubcopilot.com/models``;
    GHE.com Copilot (with data residency) serves inference and model
    listing from ``copilot-api.<org>.ghe.com``. Each instance is bound
    to a single provider key and its own endpoint so the public and
    enterprise catalogs stay distinct (different host, headers, token).
    """

    ttl_secs = DEFAULT_TTLS["github-copilot"]

    def __init__(
        self,
        *,
        provider: str,
        endpoint_base: str = "https://api.githubcopilot.com",
        endpoint: str = "https://api.githubcopilot.com/models",
    ) -> None:
        super().__init__()
        self.provider = provider
        self.endpoint_base = endpoint_base.rstrip("/")
        self.endpoint = endpoint
        # One instance per provider key; each serves only its own key.
        self.provider_keys = (provider,)

    async def fetch(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        # The Copilot catalog requires a valid Copilot API bearer.
        # On GHE hosts (``copilot-api.``) the raw OAuth token is used
        # directly — GHE bypasses the ``copilot_internal/v2/token``
        # exchange entirely (hermes-agent issue #11442). On the public
        # host the raw token is exchanged for a short-lived API token.
        from tusker_gateway.copilot_exchange import (
            copilot_request_headers,
            exchange_copilot_token,
        )
        headers = {
            "User-Agent": "tusker-gateway/1.0 (catalog-refresh)",
            "accept": "application/json",
        }
        raw_token = await self._resolve_token()
        is_ghe = "copilot-api." in self.endpoint_base.lower()
        if raw_token:
            if is_ghe:
                headers["Authorization"] = f"Bearer {raw_token}"
            else:
                try:
                    api_token, _ = await exchange_copilot_token(
                        raw_token, base_url=self.endpoint_base, http=session
                    )
                    headers["Authorization"] = f"Bearer {api_token}"
                except ValueError:
                    # Exchange failed; fall back to the raw token. Some
                    # setups accept it directly and we'd rather log a
                    # downstream 401 than crash the refresh loop.
                    headers["Authorization"] = f"Bearer {raw_token}"
            headers.update(
                copilot_request_headers(
                    base_url=self.endpoint_base,
                    is_vision=False,
                )
            )
        async with session.get(self.endpoint, headers=headers) as resp:
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
            out.append(CatalogEntry(provider=self.provider, model=slug, raw=m))
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
class ProviderModelsCatalog(CatalogClient):
    """Authenticated catalog for providers exposing an OpenAI-style list.

    A number of providers implement ``GET /models`` but do not need a
    bespoke catalog parser. The endpoint is configured on the normalized
    provider registry so custom providers can opt in without code changes.
    """

    def __init__(
        self,
        *,
        provider: str,
        endpoint: str,
        ttl_secs: float = 3600.0,
        default_input_modalities: frozenset[str] | None = None,
        default_output_modalities: frozenset[str] | None = None,
    ) -> None:
        super().__init__()
        self.provider = provider
        self.endpoint = endpoint
        self.ttl_secs = ttl_secs
        self.default_input_modalities = default_input_modalities
        self.default_output_modalities = default_output_modalities

    async def fetch(self, session: aiohttp.ClientSession) -> list[CatalogEntry]:
        headers = {
            "User-Agent": "tusker-gateway/1.0 (catalog-refresh)",
            "accept": "application/json",
        }
        token = await self._resolve_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with session.get(self.endpoint, headers=headers) as resp:
            if resp.status != 200:
                raise CatalogError(f"{self.provider} models HTTP {resp.status}")
            data = await resp.json()

        rows: list[tuple[str | None, Any]] = []
        if isinstance(data, list):
            rows = [(None, row) for row in data]
        elif isinstance(data, dict):
            collection: Any = None
            for key in ("data", "models", "items"):
                if key in data:
                    collection = data[key]
                    break
            if isinstance(collection, list):
                rows = [(None, row) for row in collection]
            elif isinstance(collection, dict):
                rows = [(str(name), row) for name, row in collection.items()]

        out: list[CatalogEntry] = []
        for fallback_name, row in rows:
            if isinstance(row, str):
                slug = row.strip()
                raw: dict[str, Any] = {"id": slug}
            elif isinstance(row, dict):
                raw = row
                slug = next(
                    (
                        row.get(key)
                        for key in ("id", "slug", "model", "name", "model_name")
                        if isinstance(row.get(key), str) and row.get(key).strip()
                    ),
                    fallback_name,
                )
                slug = slug.strip() if isinstance(slug, str) else ""
            else:
                continue
            if not slug:
                continue
            if slug.startswith("models/"):
                slug = slug[len("models/") :]
            cost_input, cost_output = _extract_catalog_pricing(raw)
            entry = CatalogEntry(
                provider=self.provider,
                model=slug,
                raw=raw,
                cost_input=cost_input,
                cost_output=cost_output,
            )
            if (
                entry.input_modalities is None
                and self.default_input_modalities is not None
                and advertised_input_modalities(entry) is None
            ):
                entry.input_modalities = self.default_input_modalities
            if (
                entry.output_modalities is None
                and self.default_output_modalities is not None
                and advertised_output_modalities(entry) is None
            ):
                entry.output_modalities = self.default_output_modalities
            out.append(entry)
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


def _extract_catalog_pricing(model_info: dict[str, Any]) -> tuple[float | None, float | None]:
    """Extract pricing from common provider catalog shapes."""
    prompt_cost, completion_cost = _extract_openrouter_pricing(model_info)
    if prompt_cost is not None or completion_cost is not None:
        return prompt_cost, completion_cost
    cost = model_info.get("cost")
    if not isinstance(cost, dict):
        cost = model_info.get("pricing")
    if not isinstance(cost, dict):
        return None, None
    return (
        _parse_cost_field(cost.get("input", cost.get("prompt"))),
        _parse_cost_field(cost.get("output", cost.get("completion"))),
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

    def __init__(
        self,
        clients: dict[str, CatalogClient] | None = None,
        model_capability_db: Any | None = None,
    ) -> None:
        self._clients: dict[str, CatalogClient] = clients or {}
        self._models_dev: ModelsDevCatalog | None = None
        self.model_capability_db = model_capability_db
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

    def providers(self) -> tuple[str, ...]:
        """Return registered provider keys in stable insertion order."""
        return tuple(self._clients)

    def _enrich_pricing(self) -> None:
        """Fill provider catalog prices from models.dev when available."""
        for client in self._clients.values():
            if isinstance(client, ModelsDevCatalog):
                continue
            for entry in client._entries:
                if entry.cost_input is not None and entry.cost_output is not None:
                    continue
                cost_input, cost_output = self._pricing_lookup(
                    entry.provider, entry.model
                )
                if entry.cost_input is None:
                    entry.cost_input = cost_input
                if entry.cost_output is None:
                    entry.cost_output = cost_output

    def _record_catalog_capabilities(self) -> None:
        """Persist explicit catalog modality claims without calling models."""
        if self.model_capability_db is None:
            return
        from tusker_gateway.model_capability import MODEL_CAPABILITY_PROBE_VERSION

        for client in self._clients.values():
            for entry in client._entries:
                for direction, modalities in (
                    ("input", advertised_input_modalities(entry)),
                    ("output", advertised_output_modalities(entry)),
                ):
                    if not modalities:
                        continue
                    for modality in modalities:
                        if modality not in {"text", "image", "audio", "video"}:
                            continue
                        self.model_capability_db.record(
                            provider=entry.provider,
                            model=entry.model,
                            capability=f"{direction}_{modality}",
                            status="advertised",
                            source="catalog",
                            probe_version=MODEL_CAPABILITY_PROBE_VERSION,
                        )

    async def refresh_all(self, session: aiohttp.ClientSession) -> None:
        """Refresh every client concurrently."""
        results = await asyncio.gather(
            *(c.refresh(session) for c in self._clients.values()),
            return_exceptions=True,
        )
        for client, res in zip(self._clients.values(), results):
            if isinstance(res, Exception):
                logger.warning("%s catalog refresh raised: %s", client.provider, res)
        self._enrich_pricing()
        self._record_catalog_capabilities()

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

    def diagnostics(self) -> dict[str, dict[str, Any]]:
        """Return credential-safe refresh state for every registered catalog."""
        return {
            provider: client.diagnostics()
            for provider, client in self._clients.items()
        }

    @classmethod
    def default(
        cls,
        provider_registry: dict[str, Any] | None = None,
        model_capability_db: Any | None = None,
    ) -> "CatalogRegistry":
        """Build catalogs for every configured provider with a model endpoint.

        Provider-specific clients handle non-standard authentication or
        response shapes. Other providers use their configured native
        ``models_path`` and the generic OpenAI-compatible parser.
        """
        if provider_registry is None:
            from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY

            provider_registry = DEFAULT_PROVIDER_REGISTRY
        reg = cls(model_capability_db=model_capability_db)
        if "openai-codex" in provider_registry:
            reg.register("openai-codex", CodexCatalog())
        if "github-copilot" in provider_registry:
            reg.register("github-copilot", CopilotCatalog(provider="github-copilot"))
        if "github-copilot-enterprise" in provider_registry:
            reg.register(
                "github-copilot-enterprise",
                CopilotCatalog(
                    provider="github-copilot-enterprise",
                    endpoint_base="https://copilot-api.sita.ghe.com",
                    endpoint="https://copilot-api.sita.ghe.com/models",
                ),
            )
        if "openrouter" in provider_registry:
            reg.register("openrouter", OpenRouterCatalog())
        if "opencode-zen" in provider_registry:
            reg.register("opencode-zen", OpenCodeCatalog())
        if "opencode-go" in provider_registry:
            reg.register("opencode-go", OpenCodeGoCatalog())
        if "xiaomi" in provider_registry:
            reg.register("xiaomi", XiaomiCatalog())

        special = {
            "openai-codex",
            "github-copilot",
            "github-copilot-enterprise",
            "openrouter",
            "opencode-zen",
            "opencode-go",
            "xiaomi",
            "models.dev",
        }
        for provider, config in provider_registry.items():
            provider = str(provider).lower()
            if provider in special:
                continue
            if isinstance(config, dict):
                models_path = config.get("models_path", config.get("catalog_path"))
                base_url = str(config.get("base_url", "")).strip()
            else:
                models_path = getattr(config, "models_path", None)
                base_url = str(getattr(config, "base_url", "")).strip()
            if not models_path:
                continue
            if not base_url:
                continue
            endpoint = str(models_path).strip()
            if not endpoint.startswith(("http://", "https://")):
                endpoint = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
            reg.register(
                provider,
                ProviderModelsCatalog(
                    provider=provider,
                    endpoint=endpoint,
                    ttl_secs=DEFAULT_TTLS.get(provider, 3600.0),
                    default_input_modalities=(
                        frozenset({"text"})
                        if provider in {"ollama-cloud", "cerebras"}
                        else None
                    ),
                    default_output_modalities=(
                        frozenset({"text"})
                        if provider in {"ollama-cloud", "cerebras"}
                        else None
                    ),
                ),
            )
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
