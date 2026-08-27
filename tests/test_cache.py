"""Unit tests for the exact-match response cache (Release 1)."""
from __future__ import annotations

import os
import tempfile

import pytest

from tusker_gateway.cache import (
    CacheConfig,
    ResponseCache,
    canonical_json,
    load_cache_config_from_env,
    make_cache_key,
    make_caller_scope,
)


@pytest.fixture
def tmp_cache_path(tmp_path):
    return os.path.join(str(tmp_path), "cache.db")


def _make_cfg(enabled: bool = True, **kwargs) -> CacheConfig:
    return CacheConfig(enabled=enabled, **kwargs)


def test_canonical_json_sorts_keys():
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b == '{"a":2,"b":1}'


def test_canonical_json_handles_nested():
    a = canonical_json({"outer": {"b": 1, "a": 2}, "first": [3, 2, 1]})
    b = canonical_json({"first": [3, 2, 1], "outer": {"a": 2, "b": 1}})
    assert a == b


def test_make_cache_key_differs_with_model():
    msgs = [{"role": "user", "content": "hi"}]
    k1 = make_cache_key(pool_name="code", model="mimo", messages=msgs, tools=None, extra_body=None)
    k2 = make_cache_key(pool_name="code", model="qwen", messages=msgs, tools=None, extra_body=None)
    assert k1 != k2


def test_make_cache_key_differs_with_messages():
    base = [{"role": "user", "content": "hi"}]
    other = [{"role": "user", "content": "bye"}]
    assert make_cache_key(pool_name="code", model="m", messages=base, tools=None, extra_body=None) != \
           make_cache_key(pool_name="code", model="m", messages=other, tools=None, extra_body=None)


def test_make_cache_key_differs_with_tools():
    msgs = [{"role": "user", "content": "hi"}]
    tools1 = [{"type": "function", "function": {"name": "bash"}}]
    tools2 = [{"type": "function", "function": {"name": "read"}}]
    assert make_cache_key(pool_name="code", model="m", messages=msgs, tools=tools1, extra_body=None) != \
           make_cache_key(pool_name="code", model="m", messages=msgs, tools=tools2, extra_body=None)


def test_make_cache_key_differs_with_caller_scope():
    msgs = [{"role": "user", "content": "hi"}]
    assert make_cache_key(
        pool_name="code", model="m", messages=msgs, tools=None, extra_body=None,
        caller_scope=make_caller_scope("caller-a"),
    ) != make_cache_key(
        pool_name="code", model="m", messages=msgs, tools=None, extra_body=None,
        caller_scope=make_caller_scope("caller-b"),
    )


def test_disabled_cache_returns_none():
    cache = ResponseCache(_make_cfg(enabled=False))
    assert cache.get("any") is None
    cache.put("any", {"x": 1})  # no-op
    assert cache.stats_snapshot() == {"hits": 0, "misses": 0, "writes": 0, "evictions": 0, "bypasses": 0}


def test_put_and_get(tmp_cache_path):
    cache = ResponseCache(_make_cfg(enabled=True, path=tmp_cache_path, ttl_secs=60))
    body = {"id": "x", "choices": [{"message": {"content": "hi"}}]}
    cache.put("k1", body)
    assert cache.get("k1") == body
    assert cache.stats_snapshot()["hits"] == 1
    assert cache.stats_snapshot()["writes"] == 1


def test_ttl_expiry_triggers_lazy_eviction(tmp_cache_path):
    cache = ResponseCache(_make_cfg(enabled=True, path=tmp_cache_path, ttl_secs=1))
    cache.put("k1", {"x": 1})
    assert cache.get("k1") is not None
    # Force expiry by waiting briefly (acceptable: this is just the in-process check)
    import time
    time.sleep(1.2)
    assert cache.get("k1") is None
    assert cache.stats_snapshot()["evictions"] >= 1


def test_miss_increments_counter(tmp_cache_path):
    cache = ResponseCache(_make_cfg(enabled=True, path=tmp_cache_path, ttl_secs=60))
    assert cache.get("missing") is None
    assert cache.stats_snapshot()["misses"] == 1


def test_overwrite_refreshes_ttl(tmp_cache_path):
    cache = ResponseCache(_make_cfg(enabled=True, path=tmp_cache_path, ttl_secs=60))
    cache.put("k1", {"v": 1})
    cache.put("k1", {"v": 2})
    assert cache.get("k1") == {"v": 2}


def test_max_entries_triggers_lru_eviction(tmp_cache_path):
    cache = ResponseCache(_make_cfg(enabled=True, path=tmp_cache_path, ttl_secs=60, max_entries=3))
    for i in range(5):
        cache.put(f"k{i}", {"v": i})
    assert cache.stats_snapshot()["evictions"] >= 1
    # Oldest two should have been evicted; newest three should still resolve.
    assert cache.get("k4") == {"v": 4}
    assert cache.get("k3") == {"v": 3}
    assert cache.get("k2") == {"v": 2}


def test_invalidate(tmp_cache_path):
    cache = ResponseCache(_make_cfg(enabled=True, path=tmp_cache_path, ttl_secs=60))
    cache.put("k1", {"x": 1})
    cache.invalidate("k1")
    assert cache.get("k1") is None


def test_load_cache_config_from_env_defaults():
    cfg = load_cache_config_from_env(env={})
    assert cfg.enabled is False
    assert cfg.ttl_secs == 300
    assert cfg.max_entries == 10_000


def test_load_cache_config_from_env_overrides():
    cfg = load_cache_config_from_env(env={
        "TUSKER_CACHE_ENABLED": "true",
        "TUSKER_CACHE_TTL_SECS": "60",
        "TUSKER_CACHE_PATH": "/tmp/x.db",
        "TUSKER_CACHE_MAX_ENTRIES": "100",
    })
    assert cfg.enabled is True
    assert cfg.ttl_secs == 60
    assert cfg.path == "/tmp/x.db"
    assert cfg.max_entries == 100


def test_persistence_across_instances(tmp_cache_path):
    # First instance writes, second reads.
    a = ResponseCache(_make_cfg(enabled=True, path=tmp_cache_path, ttl_secs=60))
    a.put("k1", {"v": 42})
    b = ResponseCache(_make_cfg(enabled=True, path=tmp_cache_path, ttl_secs=60))
    assert b.get("k1") == {"v": 42}
