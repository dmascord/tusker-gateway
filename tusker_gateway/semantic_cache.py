"""Scoped semantic response cache using ChromaDB + sentence-transformers.

Semantic caching is deliberately conservative.  Entries are isolated by a
hashed caller scope, pool, requested model, concrete provider/model, and all
forwarded generation options.  The endpoint applies additional request
eligibility rules (text-only, deterministic, no tools) before calling this
module.

Opt-in via ``TUSKER_SEMANTIC_CACHE_ENABLED=true``.  If chromadb or
sentence-transformers are not installed, the module falls back to a no-op.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency gating
# ---------------------------------------------------------------------------

_HAS_CHROMADB = False
_HAS_SENTENCE_TRANSFORMERS = False

try:
    import chromadb  # type: ignore[import-untyped]
    _HAS_CHROMADB = True
except ImportError:
    pass

try:
    from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]
    _HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    pass

_DEPS_AVAILABLE = _HAS_CHROMADB and _HAS_SENTENCE_TRANSFORMERS

_SCHEMA_VERSION = 2
_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_DEFAULT_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class SemanticCacheConfig:
    """Configuration for the semantic cache."""

    enabled: bool = False
    path: str = "data/semantic_cache"
    similarity_threshold: float = 0.92
    ttl_secs: int = 300
    max_entries: int = 5_000
    max_input_chars: int = 12_000
    max_response_bytes: int = 262_144
    model_name: str = _DEFAULT_MODEL
    model_revision: str = _DEFAULT_MODEL_REVISION
    device: str = "cpu"
    local_files_only: bool = True
    init_timeout_secs: float = 120.0
    operation_timeout_secs: float = 2.0
    max_concurrent_operations: int = 2
    require_deterministic: bool = True
    excluded_pools: tuple[str, ...] = ("privacy",)

    def __post_init__(self) -> None:
        self.similarity_threshold = min(1.0, max(0.0, float(self.similarity_threshold)))
        self.ttl_secs = max(1, int(self.ttl_secs))
        self.max_entries = max(1, int(self.max_entries))
        self.max_input_chars = max(256, int(self.max_input_chars))
        self.max_response_bytes = max(1_024, int(self.max_response_bytes))
        self.model_name = str(self.model_name or _DEFAULT_MODEL)
        self.model_revision = str(self.model_revision or "")
        if self.model_name == _DEFAULT_MODEL and not self.model_revision:
            self.model_revision = _DEFAULT_MODEL_REVISION
        self.device = str(self.device or "cpu")
        self.init_timeout_secs = max(1.0, float(self.init_timeout_secs))
        self.operation_timeout_secs = max(0.1, float(self.operation_timeout_secs))
        self.max_concurrent_operations = max(1, int(self.max_concurrent_operations))
        excluded = (
            self.excluded_pools.split(",")
            if isinstance(self.excluded_pools, str)
            else self.excluded_pools
        )
        self.excluded_pools = tuple(
            sorted({str(pool).strip().lower() for pool in excluded if str(pool).strip()})
        )


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, str(default)))
    except (TypeError, ValueError):
        logger.warning("semantic cache: invalid integer %s=%r; using %s", key, env.get(key), default)
        return default


def _env_float(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(env.get(key, str(default)))
    except (TypeError, ValueError):
        logger.warning("semantic cache: invalid number %s=%r; using %s", key, env.get(key), default)
        return default


def load_semantic_cache_config_from_env(
    env: dict[str, str] | None = None,
) -> SemanticCacheConfig:
    """Build a SemanticCacheConfig from the environment (or a dict for tests)."""
    env = env if env is not None else os.environ
    excluded_raw = env.get("TUSKER_SEMANTIC_CACHE_EXCLUDED_POOLS", "privacy")
    model_name = env.get("TUSKER_SEMANTIC_CACHE_MODEL", _DEFAULT_MODEL)
    model_revision = env.get("TUSKER_SEMANTIC_CACHE_MODEL_REVISION")
    if model_revision is None:
        model_revision = _DEFAULT_MODEL_REVISION if model_name == _DEFAULT_MODEL else ""
    return SemanticCacheConfig(
        enabled=_truthy(env.get("TUSKER_SEMANTIC_CACHE_ENABLED")),
        path=env.get("TUSKER_SEMANTIC_CACHE_PATH", "data/semantic_cache"),
        similarity_threshold=_env_float(env, "TUSKER_SEMANTIC_CACHE_THRESHOLD", 0.92),
        ttl_secs=_env_int(env, "TUSKER_SEMANTIC_CACHE_TTL", 300),
        max_entries=_env_int(env, "TUSKER_SEMANTIC_CACHE_MAX_ENTRIES", 5_000),
        max_input_chars=_env_int(env, "TUSKER_SEMANTIC_CACHE_MAX_INPUT_CHARS", 12_000),
        max_response_bytes=_env_int(env, "TUSKER_SEMANTIC_CACHE_MAX_RESPONSE_BYTES", 262_144),
        model_name=model_name,
        model_revision=model_revision,
        device=env.get("TUSKER_SEMANTIC_CACHE_DEVICE", "cpu"),
        local_files_only=_truthy(
            env.get("TUSKER_SEMANTIC_CACHE_LOCAL_FILES_ONLY"),
            default=True,
        ),
        init_timeout_secs=_env_float(env, "TUSKER_SEMANTIC_CACHE_INIT_TIMEOUT_SECS", 120.0),
        operation_timeout_secs=_env_float(env, "TUSKER_SEMANTIC_CACHE_OPERATION_TIMEOUT_SECS", 2.0),
        max_concurrent_operations=_env_int(
            env, "TUSKER_SEMANTIC_CACHE_MAX_CONCURRENT_OPERATIONS", 2
        ),
        require_deterministic=_truthy(
            env.get("TUSKER_SEMANTIC_CACHE_REQUIRE_DETERMINISTIC"),
            default=True,
        ),
        excluded_pools=tuple(
            pool.strip().lower() for pool in excluded_raw.split(",") if pool.strip()
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conversation_text(messages: list[dict[str, Any]]) -> str:
    """Extract a plain-text representation of the conversation for embedding."""
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "unknown")
        name = msg.get("name")
        speaker = f"{role}:{name}" if name else role
        parts.append(f"[{speaker}]")
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            # The endpoint rejects non-text parts before semantic caching.
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if text:
                        parts.append(text)
    return "\n".join(parts)


def _hash_text(text: str) -> str:
    """SHA-256 hash of text for stable IDs."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_semantic_scope(
    *,
    caller_scope: str,
    pool_name: str,
    requested_model: str | None,
    provider: str,
    target_model: str,
    extra_body: dict[str, Any] | None,
) -> str:
    """Return a non-reversible scope key for one cache namespace.

    ``caller_scope`` must already be a hash/fingerprint.  Keeping this helper
    in the cache module makes it difficult for callers to accidentally place a
    raw API token in Chroma metadata or logs.
    """
    payload = {
        "schema": _SCHEMA_VERSION,
        "caller": caller_scope,
        "pool": pool_name,
        "requested_model": requested_model or "",
        "provider": provider,
        "target_model": target_model,
        "options": extra_body or {},
    }
    return _hash_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def response_contains_tool_calls(value: Any) -> bool:
    """Return True when a response contains a native or requested tool call."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"tool_calls", "function_call"} and item:
                return True
            if key == "finish_reason" and item == "tool_calls":
                return True
            if response_contains_tool_calls(item):
                return True
    elif isinstance(value, list):
        return any(response_contains_tool_calls(item) for item in value)
    return False


# ---------------------------------------------------------------------------
# SemanticCache
# ---------------------------------------------------------------------------

class SemanticCache:
    """Approximate-match response cache backed by ChromaDB vectors."""

    def __init__(self, config: SemanticCacheConfig) -> None:
        self._config = config
        self._enabled = config.enabled
        self._collection: Any = None  # chromadb.Collection
        self._model: Any = None  # SentenceTransformer
        self._operation_semaphore: asyncio.Semaphore | None = None

        # Stats
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._evictions = 0
        self._errors = 0
        self._skips = 0

        if not config.enabled:
            logger.debug("semantic cache disabled by config")
            return

        if not _DEPS_AVAILABLE:
            missing = []
            if not _HAS_CHROMADB:
                missing.append("chromadb")
            if not _HAS_SENTENCE_TRANSFORMERS:
                missing.append("sentence-transformers")
            logger.warning(
                "semantic cache disabled: missing optional dependencies [%s]",
                ", ".join(missing),
            )
            self._enabled = False
            return

        # Ensure storage directory exists.
        try:
            os.makedirs(config.path, exist_ok=True)
        except (PermissionError, OSError) as exc:
            logger.warning("semantic cache disabled: cannot create %s: %s", config.path, exc)
            self._enabled = False

    # -- Properties ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def config(self) -> SemanticCacheConfig:
        return self._config

    # -- Lifecycle ------------------------------------------------------------

    async def initialize(self) -> None:
        """Load the embedding model and create/open the ChromaDB collection."""
        if not self._enabled:
            return

        logger.info(
            "semantic cache: loading embedding model=%s device=%s local_files_only=%s",
            self._config.model_name,
            self._config.device,
            self._config.local_files_only,
        )
        try:
            self._model = await asyncio.wait_for(
                asyncio.to_thread(
                    SentenceTransformer,
                    self._config.model_name,
                    device=self._config.device,
                    revision=self._config.model_revision or None,
                    local_files_only=self._config.local_files_only,
                ),
                timeout=self._config.init_timeout_secs,
            )
        except asyncio.TimeoutError:
            logger.error(
                "semantic cache: model load timed out after %.1fs; disabling",
                self._config.init_timeout_secs,
            )
            self._enabled = False
            return
        except Exception as exc:
            logger.error(
                "semantic cache: failed to load model=%s: %s; disabling",
                self._config.model_name,
                exc,
            )
            self._enabled = False
            return

        try:
            client = await asyncio.to_thread(chromadb.PersistentClient, path=self._config.path)
            model_suffix = _hash_text(
                f"{self._config.model_name}@{self._config.model_revision}"
            )[:12]
            self._collection = await asyncio.to_thread(
                client.get_or_create_collection,
                name=f"tusker_semantic_v{_SCHEMA_VERSION}_{model_suffix}",
                metadata={
                    "hnsw:space": "cosine",
                    "tusker_schema_version": _SCHEMA_VERSION,
                    "tusker_embedding_model": self._config.model_name,
                    "tusker_embedding_revision": self._config.model_revision or "unversioned",
                },
            )
            await self._prune()
            count = await self._run_blocking(self._collection.count)
            logger.info(
                "semantic cache: collection ready entries=%d threshold=%.2f ttl=%ds max_entries=%d",
                count,
                self._config.similarity_threshold,
                self._config.ttl_secs,
                self._config.max_entries,
            )
        except Exception as exc:
            logger.error("semantic cache: failed to create collection: %s; disabling", exc)
            self._enabled = False

    async def close(self) -> None:
        """Release resources (no-op for in-process ChromaDB)."""
        logger.debug("semantic cache: close (no-op)")
        # ChromaDB PersistentClient flushes on destruction; nothing to do.

    # -- Core API -------------------------------------------------------------

    async def _run_blocking(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run model/Chroma calls off the aiohttp event loop with a timeout."""
        if self._operation_semaphore is None:
            self._operation_semaphore = asyncio.Semaphore(
                self._config.max_concurrent_operations
            )
        async with self._operation_semaphore:
            return await asyncio.wait_for(
                asyncio.to_thread(func, *args, **kwargs),
                timeout=self._config.operation_timeout_secs,
            )

    def _text_for_messages(self, messages: list[dict[str, Any]]) -> str | None:
        text = _conversation_text(messages)
        if not text.strip():
            self._skips += 1
            logger.debug("semantic cache: skipped empty conversation")
            return None
        if len(text) > self._config.max_input_chars:
            self._skips += 1
            logger.debug(
                "semantic cache: skipped input_chars=%d max_input_chars=%d",
                len(text),
                self._config.max_input_chars,
            )
            return None
        return text

    async def embed_messages(self, messages: list[dict[str, Any]]) -> list[float] | None:
        """Embed a request once so a miss can reuse the vector during store."""
        if not self._enabled or self._collection is None or self._model is None:
            return None
        text = self._text_for_messages(messages)
        if text is None:
            return None
        try:
            encoded = await self._run_blocking(
                self._model.encode,
                text,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            if hasattr(encoded, "tolist"):
                encoded = encoded.tolist()
            if isinstance(encoded, list) and encoded and isinstance(encoded[0], list):
                encoded = encoded[0]
            if not isinstance(encoded, (list, tuple)) or not encoded:
                raise ValueError("embedding model returned an empty vector")
            return [float(value) for value in encoded]
        except asyncio.TimeoutError:
            self._errors += 1
            logger.warning(
                "semantic cache: embedding timed out after %.1fs",
                self._config.operation_timeout_secs,
            )
        except Exception as exc:
            self._errors += 1
            logger.warning("semantic cache: embedding failed: %s", exc)
        return None

    async def query(
        self,
        messages: list[dict[str, Any]],
        *,
        scope: str,
        embedding: list[float] | None = None,
    ) -> dict[str, Any] | None:
        """Find a cached response that is semantically similar to *messages*.

        Returns the cached response body dict, or ``None`` on miss / expired.
        """
        if not scope:
            self._skips += 1
            logger.warning("semantic cache: query skipped without a scope")
            return None
        if embedding is None:
            embedding = await self.embed_messages(messages)
        if embedding is None or not self._enabled or self._collection is None:
            return None

        # Embed and query for nearest neighbour.
        try:
            results = await self._run_blocking(
                self._collection.query,
                query_embeddings=[embedding],
                n_results=1,
                where={"scope": scope},
                include=["documents", "metadatas", "distances"],
            )
        except asyncio.TimeoutError:
            self._errors += 1
            logger.warning("semantic cache: query timed out")
            return None
        except Exception as exc:
            self._errors += 1
            logger.warning("semantic cache: query failed: %s", exc)
            return None

        ids = (results.get("ids") or [[]])[0]
        if not ids:
            self._misses += 1
            logger.debug("semantic cache: miss (no neighbours)")
            return None

        # ChromaDB cosine distance: distance=0 means identical; distance=2 means opposite.
        distances = (results.get("distances") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        distance = distances[0] if distances else 1.0
        # Convert cosine distance to similarity score (1 - distance).
        similarity = 1.0 - distance

        if similarity < self._config.similarity_threshold:
            self._misses += 1
            logger.debug(
                "semantic cache: miss (similarity=%.4f < threshold=%.2f)",
                similarity,
                self._config.similarity_threshold,
            )
            return None

        meta = metadatas[0] if metadatas else {}
        if not isinstance(meta, dict):
            meta = {}
        expires_at = meta.get("expires_at", 0)
        now = time.time()

        if expires_at and expires_at <= now:
            # Expired — evict and miss.
            doc_id = ids[0]
            try:
                await self._run_blocking(self._collection.delete, ids=[doc_id])
            except Exception:
                pass
            self._misses += 1
            self._evictions += 1
            logger.debug("semantic cache: evicted expired entry %s", doc_id)
            return None

        # Parse stored document (JSON string of response body).
        documents = (results.get("documents") or [[]])[0]
        stored_doc = documents[0] if documents else None
        if stored_doc is None:
            self._misses += 1
            logger.debug("semantic cache: miss (empty document)")
            return None

        try:
            response_body = json.loads(stored_doc)
        except (json.JSONDecodeError, TypeError):
            self._misses += 1
            logger.warning("semantic cache: miss (corrupt document)")
            return None
        if not isinstance(response_body, dict) or response_contains_tool_calls(response_body):
            try:
                await self._run_blocking(self._collection.delete, ids=[ids[0]])
            except Exception:
                pass
            self._misses += 1
            self._evictions += 1
            logger.warning("semantic cache: rejected cached tool-call or non-object response")
            return None

        self._hits += 1
        logger.info(
            "semantic cache: hit scope=%s similarity=%.4f age=%.0fs",
            scope[:12],
            similarity,
            now - meta.get("created_at", now),
        )
        return response_body

    async def store(
        self,
        messages: list[dict[str, Any]],
        response: dict[str, Any],
        *,
        scope: str,
        embedding: list[float] | None = None,
    ) -> None:
        """Embed the conversation and store the response in ChromaDB."""
        if not scope:
            self._skips += 1
            logger.warning("semantic cache: store skipped without a scope")
            return
        if not isinstance(response, dict) or response_contains_tool_calls(response):
            self._skips += 1
            logger.info("semantic cache: store skipped tool-call response")
            return
        if not self._enabled or self._collection is None:
            return

        text = self._text_for_messages(messages)
        if text is None:
            return
        if embedding is None:
            embedding = await self.embed_messages(messages)
        if embedding is None:
            return
        now = time.time()
        doc_id = _hash_text(f"{scope}\n{text}")
        request_hash = _hash_text(json.dumps(messages, sort_keys=True, separators=(",", ":")))

        document = json.dumps(response, ensure_ascii=False)
        if len(document.encode("utf-8")) > self._config.max_response_bytes:
            self._skips += 1
            logger.info(
                "semantic cache: store skipped response_bytes>%d",
                self._config.max_response_bytes,
            )
            return

        metadata = {
            "schema_version": _SCHEMA_VERSION,
            "scope": scope,
            "request_hash": request_hash,
            "created_at": now,
            "expires_at": now + self._config.ttl_secs,
        }

        try:
            await self._run_blocking(
                self._collection.upsert,
                ids=[doc_id],
                embeddings=[embedding],
                documents=[document],
                metadatas=[metadata],
            )
            self._writes += 1
            await self._prune()
            logger.debug("semantic cache: stored entry scope=%s id=%s", scope[:12], doc_id[:12])
        except asyncio.TimeoutError:
            self._errors += 1
            logger.warning("semantic cache: store timed out")
        except Exception as exc:
            self._errors += 1
            logger.warning("semantic cache: store failed: %s", exc)

    async def _prune(self) -> None:
        """Best-effort TTL and size pruning; never affects request success."""
        if self._collection is None:
            return
        try:
            count = await self._run_blocking(self._collection.count)
            if count <= self._config.max_entries:
                return
            rows = await self._run_blocking(
                self._collection.get,
                include=["metadatas"],
            )
            ids = rows.get("ids") or []
            metadatas = rows.get("metadatas") or []
            now = time.time()
            entries = []
            delete_ids: list[str] = []
            for index, doc_id in enumerate(ids):
                meta = metadatas[index] if index < len(metadatas) and metadatas[index] else {}
                if meta.get("expires_at", 0) and meta["expires_at"] <= now:
                    delete_ids.append(doc_id)
                else:
                    entries.append((float(meta.get("created_at", 0)), doc_id))
            remaining = max(0, int(count) - len(delete_ids) - self._config.max_entries)
            if remaining:
                entries.sort()
                delete_ids.extend(doc_id for _, doc_id in entries[:remaining])
            if delete_ids:
                await self._run_blocking(self._collection.delete, ids=delete_ids)
                self._evictions += len(delete_ids)
                logger.info("semantic cache: pruned entries=%d", len(delete_ids))
        except asyncio.TimeoutError:
            self._errors += 1
            logger.warning("semantic cache: prune timed out")
        except Exception as exc:
            self._errors += 1
            logger.warning("semantic cache: prune failed: %s", exc)

    # -- Stats ----------------------------------------------------------------

    def stats_snapshot(self) -> dict[str, int]:
        """Return current hit/miss/write/eviction counters."""
        return {
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "evictions": self._evictions,
            "errors": self._errors,
            "skips": self._skips,
        }


__all__ = [
    "SemanticCache",
    "SemanticCacheConfig",
    "load_semantic_cache_config_from_env",
    "make_semantic_scope",
    "response_contains_tool_calls",
]
