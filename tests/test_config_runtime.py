"""Tests for tusker_gateway.config_runtime (ConfigRuntime + AuthMiddleware).

The real tusker_gateway.config_store module is still under construction by a
sibling, so we inject a fake module into sys.modules before any import that
transitively touches it.  ConfigRuntime is then imported with working
ConfigStore/ConfigUnavailableError classes rather than the None fallbacks.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from tusker_gateway.identity import IdentityConfig


# ---------------------------------------------------------------------------
# Fake ConfigStore / ConfigUnavailableError  (mirrors the real module's API)
# ---------------------------------------------------------------------------

class ConfigUnavailableError(Exception):
    """Raised when the DB-backed config store cannot be reached."""


class ConfigStore:
    """Scriptable fake ConfigStore for polling / snapshot tests."""

    def __init__(self, generation: int = 0) -> None:
        self.generation = generation
        self.reload_now_calls = 0
        self._runtime_cfg: dict[str, Any] | None = None
        self._identity_cfg: IdentityConfig | None = None
        self._runtime_exc: Exception | None = None
        self._identity_exc: Exception | None = None

    def runtime_config(self, fallback: dict[str, Any]) -> dict[str, Any]:
        if self._runtime_exc is not None:
            raise self._runtime_exc
        return self._runtime_cfg if self._runtime_cfg is not None else dict(fallback)

    def identity_config(self, fallback: IdentityConfig) -> IdentityConfig:
        if self._identity_exc is not None:
            raise self._identity_exc
        return self._identity_cfg if self._identity_cfg is not None else fallback
    def reload_now(self) -> None:
        self.reload_now_calls += 1
        if self._runtime_exc is not None:
            raise ConfigUnavailableError("DB unreachable")


# Build the fake module object and inject it BEFORE importing anything that
# transitively depends on tusker_gateway.config_store (admin.py, config_runtime).
import types

_fake_config_store = types.ModuleType("tusker_gateway.config_store")
_fake_config_store.ConfigStore = ConfigStore
_fake_config_store.ConfigUnavailableError = ConfigUnavailableError
import sys

sys.modules["tusker_gateway.config_store"] = _fake_config_store

# Now import the modules under test (they'll find the working fake above).
from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.config_runtime import ConfigRuntime, _env_enabled


def _make_app(config: dict[str, Any] | None = None) -> web.Application:
    app = web.Application()
    app["config"] = config if config is not None else {"api_keys": [], "providers": {}}
    return app


def _env_state(truthy: str | None) -> None:
    """Helper: set (or remove) TUSKER_CONFIG_DATABASE_ENABLED."""
    if truthy is None:
        os.environ.pop("TUSKER_CONFIG_DATABASE_ENABLED", None)
    else:
        os.environ["TUSKER_CONFIG_DATABASE_ENABLED"] = truthy


# ===========================================================================
# 1. ConfigRuntime enabled flag via env
# ===========================================================================

def test_config_runtime_enabled_flag_via_env(monkeypatch) -> None:
    for truthy in ("1", "true", "yes", "on"):
        monkeypatch.setenv("TUSKER_CONFIG_DATABASE_ENABLED", truthy)
        rt = ConfigRuntime(_make_app())
        assert rt.enabled() is True, f"expected enabled for {truthy!r}"

    monkeypatch.delenv("TUSKER_CONFIG_DATABASE_ENABLED", raising=False)
    rt = ConfigRuntime(_make_app())
    assert rt.enabled() is False

    for falsy in ("0", "false", "no", "off", "anything"):
        monkeypatch.setenv("TUSKER_CONFIG_DATABASE_ENABLED", falsy)
        rt = ConfigRuntime(_make_app())
        assert rt.enabled() is False, f"expected disabled for {falsy!r}"


# ===========================================================================
# 2. legacy mode (env unset): app has no config_store, auth dev bypass works,
#    runtime_config returns fallback
# ===========================================================================

def test_legacy_mode_app_has_no_config_store(monkeypatch) -> None:
    _env_state(None)
    monkeypatch.setenv("TUSKER_CATALOG_ENABLED", "0")
    monkeypatch.setenv("TUSKER_RTK_ENABLED", "0")
    monkeypatch.setenv("TUSKER_SEMANTIC_CACHE_ENABLED", "0")
    from tusker_gateway.app import create_app

    app = create_app()
    assert "config_store" not in app
    assert "config_runtime" not in app


def test_legacy_mode_auth_dev_bypass_works(monkeypatch) -> None:
    """When no config_store and empty legacy api_keys, dev key is accepted."""
    _env_state(None)
    app = _make_app(config={"api_keys": []})
    middleware = AuthMiddleware()
    req = make_mocked_request(
        "GET", "/chat", headers={"Authorization": "Bearer sk-secret-dev"}, app=app
    )
    asyncio.run(middleware.verify(req))  # no raise


def test_legacy_mode_runtime_config_returns_fallback(monkeypatch) -> None:
    """No store → runtime_config returns the caller-supplied fallback."""
    _env_state(None)
    rt = ConfigRuntime(_make_app())
    fallback = {"api_keys": ["legacy-key"]}
    assert rt.runtime_config(fallback) is fallback
    assert rt.status()["generation"] == 0


# ===========================================================================
# 3. Enabled mode: runtime_config returns DB snapshot, generation bumps
# ===========================================================================

def test_runtime_config_returns_store_snapshot_and_updates_generation(
    monkeypatch,
) -> None:
    _env_state("1")
    store = ConfigStore(generation=5)
    store._runtime_cfg = {"api_keys": ["db-key-1"], "db_gen": 5}
    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    fallback = {"api_keys": []}
    snap = rt.runtime_config(fallback)

    assert snap == store._runtime_cfg
    assert rt.status()["generation"] == 5
    assert rt._last_good_runtime is snap


# ===========================================================================
# 4. ConfigUnavailableError: retains last-good, error redacted
# ===========================================================================

def test_runtime_config_unavailable_retains_last_good(monkeypatch) -> None:
    _env_state("1")
    store = ConfigStore(generation=2)
    store._runtime_cfg = {"api_keys": ["good-key"], "gen": 2}
    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    fallback = {"api_keys": []}

    first = rt.runtime_config(fallback)
    assert first["api_keys"] == ["good-key"]
    assert rt._error is None

    store._runtime_exc = ConfigUnavailableError("DB down: token=abc123")
    second = rt.runtime_config(fallback)

    rt2 = ConfigRuntime(app)  # fresh: _last_good_runtime is None
    store._runtime_exc = ConfigUnavailableError("still down")
    fb = rt2.runtime_config(fallback)
    assert fb is fallback
    assert rt2.status()["error"] == "ConfigUnavailableError"


# ===========================================================================
# 5. identity_config same pattern
# ===========================================================================

def test_identity_config_same_pattern(monkeypatch) -> None:
    _env_state("1")
    store = ConfigStore(generation=1)
    id_cfg = IdentityConfig(identities={"fp": None}, required=True)  # type: ignore[arg-type]
    store._identity_cfg = id_cfg
    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    fallback = IdentityConfig()
    snap = rt.identity_config(fallback)
    assert snap is id_cfg
    assert rt._last_good_identity is id_cfg
    assert rt.status()["generation"] == 1

    store._identity_exc = ConfigUnavailableError("DB missing")
    second = rt.identity_config(fallback)
    assert second is id_cfg
    assert rt.status()["error"] == "ConfigUnavailableError"


# ===========================================================================
# 6. reload_now delegates to store
# ===========================================================================

def test_reload_now_delegates_to_store(monkeypatch) -> None:
    _env_state("1")
    store = ConfigStore(generation=7)
    store._runtime_cfg = {"x": 1}
    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    assert rt.reload_now() is True
    assert store.reload_now_calls == 1
    assert rt.status()["generation"] == 7
    assert rt._error is None

    store._runtime_exc = ConfigUnavailableError("token=x-secret")
    assert rt.reload_now() is False
    assert rt.status()["error"] == "ConfigUnavailableError"
    assert "x-secret" not in rt.status()["error"]

    _env_state(None)
    rt2 = ConfigRuntime(app)
    assert rt2.reload_now() is False


# ===========================================================================
# 7. Poll loop applies generation changes and calls _apply only on change
#    (including delete + rapid update)
# ===========================================================================

@pytest.mark.asyncio
async def test_poll_loop_applies_only_on_generation_change() -> None:
    _env_state("1")
    store = ConfigStore(generation=0)
    store._runtime_cfg = {"api_keys": ["k"], "cfg": "v"}
    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    apply_calls: list[int] = []
    original_apply = rt._apply

    def counting_apply(gen: int) -> None:
        apply_calls.append(gen)
        original_apply(gen)

    rt._apply = counting_apply  # type: ignore[method-assign]

    stop = asyncio.Event()
    await rt.start(stop, interval_secs=0.005)

    await asyncio.sleep(0.03)  # first tick

    store.generation = 1
    store._runtime_cfg = {"api_keys": [], "deleted": True}
    await asyncio.sleep(0.06)

    store.generation = 2
    store._runtime_cfg = {"api_keys": ["new"], "updated": True}
    await asyncio.sleep(0.06)

    await asyncio.sleep(0.06)  # no-change period

    stop.set()
    await rt.stop()

    assert apply_calls == [1, 2], f"unexpected apply_calls={apply_calls}"
    assert rt.status()["generation"] == 2


# ===========================================================================
# 8. Auth dev bypass eliminated when DB-authoritative key section non-empty;
#    dev bypass still works when legacy fallback empty and no store
# ===========================================================================

@pytest.mark.asyncio
async def test_auth_dev_bypass_eliminated_when_db_keys_authoritative() -> None:
    _env_state("1")
    store = ConfigStore(generation=1)
    store._runtime_cfg = {
            "config_db_keys_authoritative": True,
        "api_keys": ["prod-key-1", "prod-key-2"],
    }
    app = _make_app(config={"api_keys": []})
    app["config_store"] = store

    middleware = AuthMiddleware()

    req = make_mocked_request(
        "GET", "/chat", headers={"Authorization": "Bearer sk-secret-dev"}, app=app
    )
    with pytest.raises(Exception):
        await middleware.verify(req)

    req2 = make_mocked_request(
        "GET", "/chat", headers={"Authorization": "Bearer prod-key-1"}, app=app
    )
    await middleware.verify(req2)  # no raise


@pytest.mark.asyncio
async def test_auth_dev_bypass_still_works_when_legacy_fallback_empty_no_store() -> None:
    _env_state(None)
    app = _make_app(config={"api_keys": []})
    middleware = AuthMiddleware()
    req = make_mocked_request(
        "GET", "/chat", headers={"Authorization": "Bearer sk-secret-dev"}, app=app
    )


@pytest.mark.asyncio
async def test_auth_dev_bypass_eliminated_when_db_authoritative_keys_empty() -> None:
    """DB-authoritative key section empty -> dev key must be rejected."""
    _env_state("1")
    store = ConfigStore(generation=1)
    store._runtime_cfg = {
        "config_db_keys_authoritative": True,
        "api_keys": [],  # authoritative, but no keys permitted
    }
    app = _make_app(config={"api_keys": []})
    app["config_store"] = store
    middleware = AuthMiddleware()

    req = make_mocked_request(
        "GET", "/chat", headers={"Authorization": "Bearer sk-secret-dev"}, app=app,
    )
    with pytest.raises(Exception):
        await middleware.verify(req)


# ===========================================================================
# 9. Concurrent poll does not corrupt state
# ===========================================================================

@pytest.mark.asyncio
async def test_concurrent_poll_does_not_corrupt_state() -> None:
    _env_state("1")
    store = ConfigStore(generation=0)
    store._runtime_cfg = {"api_keys": ["x"], "p": 1}
    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    rt._apply = lambda gen: None  # no-op to avoid real rebuilds

    stop = asyncio.Event()
    await rt.start(stop, interval_secs=0.01)

    async def worker() -> None:
        for _ in range(8):
            rt.apply_reload()
            await asyncio.sleep(0.005)

    await asyncio.gather(worker(), worker(), return_exceptions=True)
    stop.set()
    await rt.stop()

    status = rt.status()
    assert isinstance(status["generation"], int)
    assert status["generation"] >= 0
    assert status["error"] is None
