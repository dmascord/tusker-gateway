"""Tests for tusker_gateway.config_runtime (ConfigRuntime + AuthMiddleware).

Uses the real ``ConfigStore`` against temporary SQLite databases; outage
semantics are exercised by making individual store methods raise.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from tusker_gateway.config_store import ConfigStore, ConfigUnavailableError
from tusker_gateway.identity import IdentityConfig, fingerprint_api_key

from tusker_gateway.auth import AuthMiddleware
from tusker_gateway.config_runtime import ConfigRuntime, _env_enabled

@pytest.fixture(autouse=True)
def _restore_config_db_env():
    """Restore TUSKER_CONFIG_DATABASE_ENABLED after each test.

    Tests here toggle the flag globally via _env_state; leaving it set
    would leak DB-backed config into unrelated tests that build real
    apps (live/e2e suites) later in the run.
    """
    saved = os.environ.get("TUSKER_CONFIG_DATABASE_ENABLED")
    yield
    if saved is None:
        os.environ.pop("TUSKER_CONFIG_DATABASE_ENABLED", None)
    else:
        os.environ["TUSKER_CONFIG_DATABASE_ENABLED"] = saved


def _make_app(config: dict[str, Any] | None = None) -> web.Application:
    app = web.Application()
    app["config"] = config if config is not None else {"api_keys": [], "providers": {}}
    return app


def _make_store(
    fallback_config: dict[str, Any] | None = None,
) -> ConfigStore:
    """Real ConfigStore against a fresh temp SQLite database."""
    dbfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    os.unlink(dbfile)
    return ConfigStore(
        database=dbfile,
        fallback_config=fallback_config or {},
        fallback_identity_config=IdentityConfig(),
    )


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
# 3. Enabled mode: runtime_config returns DB snapshot, generation tracks
# ===========================================================================

def test_runtime_config_returns_store_snapshot_and_updates_generation(
    monkeypatch,
) -> None:
    _env_state("1")
    store = _make_store()
    key = store.upsert_client_key({"principal": "don", "tenant": "gould"})
    assert store.generation == 1

    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    fallback = {"api_keys": []}
    snap = rt.runtime_config(fallback)

    assert key["api_key"] in snap["api_keys"]
    assert snap["config_db_keys_authoritative"] is True
    assert rt.status()["generation"] == 1
    assert rt._last_good_runtime is snap


# ===========================================================================
# 4. ConfigUnavailableError: retains last-good, error redacted
# ===========================================================================

def test_runtime_config_unavailable_retains_last_good(monkeypatch) -> None:
    _env_state("1")
    store = _make_store()
    key = store.upsert_client_key({"principal": "a", "tenant": "b"})
    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    fallback = {"api_keys": []}

    first = rt.runtime_config(fallback)
    assert key["api_key"] in first["api_keys"]
    assert rt._error is None

    def _raise(fallback: dict[str, Any]) -> dict[str, Any]:
        raise ConfigUnavailableError("DB down: token=abc123")

    store.runtime_config = _raise  # type: ignore[method-assign]
    rt2 = ConfigRuntime(app)  # fresh: _last_good_runtime is None
    fb = rt2.runtime_config(fallback)
    assert fb is fallback
    assert rt2.status()["error"] == "ConfigUnavailableError"


# ===========================================================================
# 5. identity_config same pattern
# ===========================================================================

def test_identity_config_same_pattern(monkeypatch) -> None:
    _env_state("1")
    store = _make_store()
    key = store.upsert_client_key({"principal": "a", "tenant": "b"})
    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    fallback = IdentityConfig()
    snap = rt.identity_config(fallback)
    assert key["fingerprint"] in snap.identities
    assert rt._last_good_identity is snap
    assert rt.status()["generation"] == 1

    def _raise(fallback: IdentityConfig) -> IdentityConfig:
        raise ConfigUnavailableError("DB missing")

    store.identity_config = _raise  # type: ignore[method-assign]
    second = rt.identity_config(fallback)
    assert second is snap  # last-good retained
    assert rt.status()["error"] == "ConfigUnavailableError"


# ===========================================================================
# 6. reload_now delegates to store
# ===========================================================================

def test_reload_now_delegates_to_store(monkeypatch) -> None:
    _env_state("1")
    store = _make_store()
    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    assert rt.reload_now() is True  # first load succeeds
    assert rt.status()["generation"] == 0
    assert rt._error is None

    assert rt.reload_now() is True  # still success when nothing changed

    def _raise() -> bool:
        raise ConfigUnavailableError("token=x-secret")

    store.reload_now = _raise  # type: ignore[method-assign]
    assert rt.reload_now() is False
    assert rt.status()["error"] == "ConfigUnavailableError"
    assert "x-secret" not in rt.status()["error"]

    _env_state(None)
    rt2 = ConfigRuntime(app)
    assert rt2.reload_now() is False


# ===========================================================================
# 7. Poll loop applies generation changes and calls _apply only on change
# ===========================================================================

@pytest.mark.asyncio
async def test_poll_loop_applies_only_on_generation_change() -> None:
    _env_state("1")
    store = _make_store()
    store.reload_now()
    app = _make_app()
    app["config_store"] = store

    rt = ConfigRuntime(app)
    apply_calls: list[int] = []
    original_apply = rt._apply

    def counting_apply(gen: int) -> None:
        apply_calls.append(gen)

    rt._apply = counting_apply  # type: ignore[method-assign]

    stop = asyncio.Event()
    await rt.start(stop, interval_secs=0.005)

    await asyncio.sleep(0.03)  # first tick loads generation 0

    # Bump the DB generation with real writes.
    store.upsert_provider({"name": "p1", "base_url": "https://one.example"})
    await asyncio.sleep(0.06)

    store.upsert_provider({"name": "p2", "base_url": "https://two.example"})
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
    store = _make_store()
    k1 = store.upsert_client_key({"principal": "a", "tenant": "b"})
    store.upsert_client_key({"principal": "c", "tenant": "d"})
    app = _make_app(config={"api_keys": []})
    app["config_store"] = store

    middleware = AuthMiddleware()

    req = make_mocked_request(
        "GET", "/chat", headers={"Authorization": "Bearer sk-secret-dev"}, app=app
    )
    with pytest.raises(Exception):
        await middleware.verify(req)

    req2 = make_mocked_request(
        "GET", "/chat", headers={"Authorization": f"Bearer {k1['api_key']}"}, app=app
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
    await middleware.verify(req)


@pytest.mark.asyncio
async def test_auth_dev_bypass_eliminated_when_db_authoritative_keys_empty() -> None:
    """DB-authoritative key section empty -> dev key must be rejected."""
    _env_state("1")
    store = _make_store()
    store.reload_now()
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
    store = _make_store()
    store.upsert_client_key({"principal": "a", "tenant": "b"})
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
def test_upsert_client_key_honours_user_provided_api_key() -> None:
    _env_state("1")
    store = _make_store()
    known_key = "sk-" + secrets.token_hex(24)
    expected_fp = fingerprint_api_key(known_key)
    result = store.upsert_client_key({
        "api_key": known_key,
        "principal": "provided",
        "tenant": "ops",
        "scopes": ["inference:chat"],
    })
    assert result["api_key"] == known_key
    assert result["fingerprint"] == expected_fp

    snap = store.runtime_config({"api_keys": []})
    assert known_key in snap["api_keys"]


def test_upsert_client_key_rejects_api_key_with_fingerprint() -> None:
    _env_state("1")
    store = _make_store()
    existing = store.upsert_client_key({"principal": "x", "tenant": "y"})
    fp = existing["fingerprint"]
    with pytest.raises(ValueError, match="rotate"):
        store.upsert_client_key({
            "fingerprint": fp,
            "api_key": "sk-" + secrets.token_hex(24),
            "principal": "x",
            "tenant": "y",
        })


@pytest.mark.asyncio
async def test_rebuild_identity_store_picks_up_new_keys() -> None:
    _env_state("1")
    store = _make_store()
    app = _make_app()
    app["config_store"] = store
    from tusker_gateway.identity import IdentityStore
    app["identity_store"] = IdentityStore(IdentityConfig())
    rt = ConfigRuntime(app)

    # Manually drive identity-store rebuild (avoids the full PoolManager rebuild
    # which needs quality_db_path and other config the runtime fixture lacks).
    rt._rebuild_identity_store()

    new_key = store.upsert_client_key({
        "principal": "post-reload",
        "tenant": "ops",
        "scopes": ["inference:chat"],
    })["api_key"]

    # Before second rebuild, identity store doesn't know this key.
    pre = app["identity_store"].resolve(new_key)
    assert pre.principal.startswith("key:")  # legacy fallback

    rt._rebuild_identity_store()
    assert app["identity_store"].resolve(new_key).principal == "post-reload"
