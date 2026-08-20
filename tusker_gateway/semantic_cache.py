"""Semantic response cache using ChromaDB + sentence-transformers.

Provides approximate-match caching for chat-completion responses.  When a
new request is semantically similar to a previously cached one, the stored
response is returned without calling the upstream provider.

Opt-in via TUSKER_SEMANTIC_CACHE_ENABLED=true.  If chromadb or
sentence-transformers are not installed, the module falls back to a
no-op (enabled=False).
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class SemanticCacheConfig:
    """Configuration for the semantic cache."""

    enabled: bool = False
    path: str = "data/semantic_cache"
    similarity_threshold: float = 0.85
    ttl_secs: int = 3600
    model_name: str = "all-MiniLM-L6-v2"


def load_semantic_cache_config_from_env(
    env: dict[str, str] | None = None,
) -> SemanticCacheConfig:
    """Build a SemanticCacheConfig from the environment (or a dict for tests)."""
    env = env if env is not None else os.environ
    enabled_raw = env.get("TUSKER_SEMANTIC_CACHE_ENABLED", "false").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    return SemanticCacheConfig(
        enabled=enabled,
        path=env.get("TUSKER_SEMANTIC_CACHE_PATH", "data/semantic_cache"),
        similarity_threshold=float(env.get("TUSKER_SEMANTIC_CACHE_THRESHOLD", "0.85")),
        ttl_secs=int(env.get("TUSKER_SEMANTIC_CACHE_TTL", "3600")),
        model_name=env.get("TUSKER_SEMANTIC_CACHE_MODEL", "all-MiniLM-L6-v2"),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conversation_text(messages: list[dict[str, Any]]) -> str:
    """Extract a plain-text representation of the conversation for embedding."""
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            # Multi-part content (vision, tool results, etc.)
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if text:
                        parts.append(text)
    return "\n".join(parts)


def _hash_text(text: str) -> str:
    """SHA-256 hash of text for stable IDs."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

        # Stats
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._evictions = 0

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

    # -- Lifecycle ------------------------------------------------------------

    async def initialize(self) -> None:
        """Load the embedding model and create/open the ChromaDB collection."""
        if not self._enabled:
            return

        logger.info("semantic cache: loading embedding model '%s'", self._config.model_name)
        try:
            self._model = SentenceTransformer(self._config.model_name)
        except Exception as exc:
            logger.error("semantic cache: failed to load model '%s': %s", self._config.model_name, exc)
            self._enabled = False
            return

        try:
            client = chromadb.PersistentClient(path=self._config.path)
            self._collection = client.get_or_create_collection(
                name="tusker_semantic_cache",
                metadata={"hnsw:space": "cosine"},
            )
            count = self._collection.count()
            logger.info(
                "semantic cache: collection ready (%d existing entries, threshold=%.2f, ttl=%ds)",
                count,
                self._config.similarity_threshold,
                self._config.ttl_secs,
            )
        except Exception as exc:
            logger.error("semantic cache: failed to create ChromaDB collection: %s", exc)
            self._enabled = False

    async def close(self) -> None:
        """Release resources (no-op for in-process ChromaDB)."""
        logger.debug("semantic cache: close (no-op)")
        # ChromaDB PersistentClient flushes on destruction; nothing to do.

    # -- Core API -------------------------------------------------------------

    async def query(self, messages: list[dict[str, Any]]) -> dict | None:
        """Find a cached response that is semantically similar to *messages*.

        Returns the cached response body dict, or ``None`` on miss / expired.
        """
        if not self._enabled or self._collection is None or self._model is None:
            return None

        text = _conversation_text(messages)
        if not text.strip():
            logger.debug("semantic cache: query skipped (empty conversation)")
            return None

        # Embed and query for nearest neighbour.
        embedding = self._model.encode(text).tolist()
        try:
            results = self._collection.query(
                query_embeddings=[embedding],
                n_results=1,
                include=["documents", "metadatas"],
            )
        except Exception as exc:
            logger.warning("semantic cache: query failed: %s", exc)
            return None

        ids = results.get("ids", [[]])[0]
        if not ids:
            self._misses += 1
            logger.debug("semantic cache: miss (no neighbours)")
            return None

        # ChromaDB cosine distance: distance=0 means identical; distance=2 means opposite.
        distances = results.get("distances", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
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
        expires_at = meta.get("expires_at", 0)
        now = time.time()

        if expires_at and expires_at <= now:
            # Expired — evict and miss.
            doc_id = ids[0]
            try:
                self._collection.delete(ids=[doc_id])
            except Exception:
                pass
            self._misses += 1
            self._evictions += 1
            logger.debug("semantic cache: evicted expired entry %s", doc_id)
            return None

        # Parse stored document (JSON string of response body).
        documents = results.get("documents", [[]])[0]
        stored_doc = documents[0] if documents else None
        if stored_doc is None:
            self._misses += 1
            logger.debug("semantic cache: miss (empty document)")
            return None

        import json
        try:
            response_body = json.loads(stored_doc)
        except (json.JSONDecodeError, TypeError):
            self._misses += 1
            logger.warning("semantic cache: miss (corrupt document)")
            return None

        self._hits += 1
        logger.info(
            "semantic cache: hit (similarity=%.4f, age=%.0fs)",
            similarity,
            now - meta.get("created_at", now),
        )
        return response_body

    async def store(
        self,
        messages: list[dict[str, Any]],
        response: dict[str, Any],
    ) -> None:
        """Embed the conversation and store the response in ChromaDB."""
        if not self._enabled or self._collection is None or self._model is None:
            return

        text = _conversation_text(messages)
        if not text.strip():
            logger.debug("semantic cache: store skipped (empty conversation)")
            return

        embedding = self._model.encode(text).tolist()
        now = time.time()
        doc_id = _hash_text(text)
        request_hash = hashlib.sha256(
            str(messages).encode("utf-8")
        ).hexdigest()

        import json
        document = json.dumps(response, ensure_ascii=False)

        metadata = {
            "request_hash": request_hash,
            "created_at": now,
            "expires_at": now + self._config.ttl_secs,
        }

        try:
            self._collection.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[document],
                metadatas=[metadata],
            )
            self._writes += 1
            logger.debug("semantic cache: stored entry %s", doc_id)
        except Exception as exc:
            logger.warning("semantic cache: store failed: %s", exc)

    # -- Stats ----------------------------------------------------------------

    def stats_snapshot(self) -> dict[str, int]:
        """Return current hit/miss/write/eviction counters."""
        return {
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "evictions": self._evictions,
        }


__all__ = [
    "SemanticCache",
    "SemanticCacheConfig",
    "load_semantic_cache_config_from_env",
]
