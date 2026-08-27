"""Safety and lifecycle tests for the scoped semantic cache."""
from __future__ import annotations

import json

import pytest

import tusker_gateway.semantic_cache as semantic_cache_module
from tusker_gateway.semantic_cache import (
    SemanticCache,
    SemanticCacheConfig,
    load_semantic_cache_config_from_env,
    make_semantic_scope,
    response_contains_tool_calls,
)


class _FakeModel:
    def __init__(self) -> None:
        self.calls = 0

    def encode(self, text, **kwargs):
        self.calls += 1
        assert kwargs["convert_to_numpy"] is True
        assert kwargs["show_progress_bar"] is False
        return [1.0, 0.0]


class _FakeCollection:
    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        self.queries: list[dict] = []

    def count(self):
        return len(self.entries)

    def query(self, **kwargs):
        self.queries.append(kwargs)
        scope = kwargs["where"]["scope"]
        for doc_id, entry in self.entries.items():
            if entry["metadatas"]["scope"] == scope:
                return {
                    "ids": [[doc_id]],
                    "distances": [[0.0]],
                    "metadatas": [[entry["metadatas"]]],
                    "documents": [[entry["documents"]]],
                }
        return {"ids": [[]], "distances": [[]], "metadatas": [[]], "documents": [[]]}

    def upsert(self, *, ids, embeddings, documents, metadatas):
        self.entries[ids[0]] = {
            "embeddings": embeddings[0],
            "documents": documents[0],
            "metadatas": metadatas[0],
        }

    def get(self, **kwargs):
        return {
            "ids": list(self.entries),
            "metadatas": [entry["metadatas"] for entry in self.entries.values()],
        }

    def delete(self, *, ids):
        for doc_id in ids:
            self.entries.pop(doc_id, None)


def _scope(*, caller: str = "caller-a", provider: str = "openai", model: str = "gpt-4o", options=None):
    return make_semantic_scope(
        caller_scope=caller,
        pool_name="code",
        requested_model="hermes-code",
        provider=provider,
        target_model=model,
        extra_body=options,
    )


@pytest.fixture
def fake_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(semantic_cache_module, "_DEPS_AVAILABLE", True)
    cache = SemanticCache(
        SemanticCacheConfig(
            enabled=True,
            path=str(tmp_path / "semantic"),
            operation_timeout_secs=1,
            max_entries=10,
        )
    )
    cache._model = _FakeModel()
    cache._collection = _FakeCollection()
    return cache


@pytest.mark.asyncio
async def test_semantic_cache_reuses_one_embedding_and_filters_by_scope(fake_cache):
    messages = [{"role": "user", "content": "What is the capital of Australia?"}]
    scope_a = _scope()
    scope_b = _scope(caller="caller-b")

    embedding = await fake_cache.embed_messages(messages)
    await fake_cache.store(messages, {"choices": [{"message": {"content": "Canberra"}}]}, scope=scope_a, embedding=embedding)
    assert fake_cache._model.calls == 1

    hit = await fake_cache.query(messages, scope=scope_a, embedding=embedding)
    miss = await fake_cache.query(messages, scope=scope_b, embedding=embedding)

    assert hit["choices"][0]["message"]["content"] == "Canberra"
    assert miss is None
    assert fake_cache._collection.queries[0]["where"] == {"scope": scope_a}
    assert fake_cache._collection.queries[1]["where"] == {"scope": scope_b}


@pytest.mark.asyncio
async def test_semantic_cache_scope_changes_for_route_and_generation_options():
    base = _scope()
    assert base != _scope(provider="github-copilot")
    assert base != _scope(model="gpt-5.5")
    assert base != _scope(options={"temperature": 0, "max_tokens": 100})


@pytest.mark.asyncio
async def test_semantic_cache_rejects_tool_call_responses(fake_cache):
    messages = [{"role": "user", "content": "run the command"}]
    await fake_cache.store(
        messages,
        {
            "choices": [{
                "message": {"tool_calls": [{"id": "call-1"}]},
                "finish_reason": "tool_calls",
            }]
        },
        scope=_scope(),
    )
    assert fake_cache._collection.entries == {}
    assert fake_cache.stats_snapshot()["skips"] == 1


def test_response_contains_tool_calls_is_recursive():
    assert response_contains_tool_calls({"choices": [{"message": {"tool_calls": [{"id": "x"}]}}]})
    assert response_contains_tool_calls({"choices": [{"finish_reason": "tool_calls"}]})
    assert not response_contains_tool_calls({"choices": [{"message": {"content": "done"}}]})


def test_semantic_config_has_safe_defaults_and_handles_bad_values():
    cfg = load_semantic_cache_config_from_env({
        "TUSKER_SEMANTIC_CACHE_ENABLED": "true",
        "TUSKER_SEMANTIC_CACHE_THRESHOLD": "not-a-number",
        "TUSKER_SEMANTIC_CACHE_TTL": "0",
        "TUSKER_SEMANTIC_CACHE_MAX_ENTRIES": "0",
        "TUSKER_SEMANTIC_CACHE_LOCAL_FILES_ONLY": "true",
    })
    assert cfg.enabled is True
    assert cfg.similarity_threshold == 0.92
    assert cfg.ttl_secs == 1
    assert cfg.max_entries == 1
    assert cfg.local_files_only is True
    assert cfg.model_revision
    assert cfg.require_deterministic is True
    assert cfg.excluded_pools == ("privacy",)
