"""Regression coverage for the gateway follow-up fixes (2026-11-25).

Each test here fails against the unfixed tree:

- the direct-route cooldown gate now also covers ``POST /v1/messages``;
- a rotator created by a configuration reload receives the cross-process
  OAuth refresh lock;
- the SQLite advisory-lock path shares one mutex per credential key;
- the provider-health payload reports capacity-group quarantines and does
  not report logical capacity groups as providers;
- the scheduled retention prune runs independently of the qualification
  maintenance switches;
- the deploy script applies the logical backup manifest and the backup
  command fails closed when ``pg_dump`` fails.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import HEADERS_AUTH

REPO = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. /v1/messages honours the direct-route cooldown gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_direct_route_in_cooldown_does_not_reach_upstream(
    app, client
):
    """An explicit Anthropic route in cooldown must fail locally.

    The OpenAI chat path gained this gate in PR #85; the Anthropic direct
    passthrough branch had its own dispatch path and kept calling upstream
    with a quarantined route.
    """
    from tusker_gateway.cooldown import global_tracker

    global_tracker().cooldown("zai", "glm-5.3-flash", 3600)

    with patch(
        "tusker_gateway.anthropic_adapter.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        response = await client.post(
            "/v1/messages",
            json={
                "model": "zai/glm-5.3-flash",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=HEADERS_AUTH,
        )

    assert response.status == 429
    assert int(response.headers["Retry-After"]) > 3000
    mock_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_anthropic_direct_route_outside_cooldown_still_dispatches(app, client):
    """The gate must not block an eligible route."""
    with patch(
        "tusker_gateway.anthropic_adapter.PassthroughClient.chat",
        new_callable=AsyncMock,
    ) as mock_chat:
        mock_chat.return_value = {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello there!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        response = await client.post(
            "/v1/messages",
            json={
                "model": "zai/glm-5.3-flash",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=HEADERS_AUTH,
        )

    assert response.status == 200
    mock_chat.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. A rotator built by a config reload gets the refresh lock
# ---------------------------------------------------------------------------


def test_rebuild_rotators_passes_the_refresh_lock(monkeypatch):
    """Rotators created after a reload must use the DB advisory lock.

    Without it a reloaded rotator falls back to a process-local lock, and
    the two gateway replicas can consume the same single-use refresh token.
    """
    from tusker_gateway.config_runtime import ConfigRuntime
    from tusker_gateway.passthrough import CodexTokenRotator

    from .test_config_runtime import _make_app, _make_store

    store = _make_store()
    lock_calls: list[int] = []

    def _fake_lock(key: int):
        lock_calls.append(key)

        class _Ctx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    monkeypatch.setattr(
        type(store),
        "oauth_refresh_lock",
        property(lambda self: _fake_lock),
        raising=False,
    )
    monkeypatch.setattr(
        store,
        "runtime_config",
        lambda fallback: {
            "credential_pools": {
                "openai-codex": [{"access_token": "a", "refresh_token": "r"}],
            },
            "auth_file": None,
            "providers": {},
        },
    )

    app = _make_app({"providers": {}})
    app["config_store"] = store
    runtime = ConfigRuntime(app)
    runtime._last_media_providers = frozenset()

    runtime._rebuild_rotators()

    rotator = app["credential_rotators"]["openai-codex"]
    assert isinstance(rotator, CodexTokenRotator)
    # The rotator holds the store callback, so its RefreshLock can reach the
    # PostgreSQL advisory lock instead of the process-local fallback.
    assert rotator._refresh_lock._acquire is not None


# ---------------------------------------------------------------------------
# 3. The SQLite advisory lock is shared, not per call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_advisory_lock_serializes_two_callers(monkeypatch):
    """Two rotators on one SQLite store must not enter the section at once.

    The old code created a fresh ``threading.Lock()`` per invocation, so no
    two callers ever contended.
    """
    from tusker_gateway.config_store import ConfigStore

    dbfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    os.unlink(dbfile)
    monkeypatch.setenv("TUSKER_STORAGE_MODE", "sqlite")
    store = ConfigStore(database=dbfile)

    order: list[str] = []
    entered = asyncio.Event()

    async def _first():
        async with store.oauth_refresh_advisory_lock(4242):
            order.append("first-in")
            entered.set()
            await asyncio.sleep(0.05)
            order.append("first-out")

    async def _second():
        async with store.oauth_refresh_advisory_lock(4242):
            order.append("second-in")

    first = asyncio.create_task(_first())
    await entered.wait()
    second = asyncio.create_task(_second())
    await asyncio.sleep(0.01)
    # The second caller must still be waiting for the first to release.
    assert order == ["first-in"]
    await asyncio.gather(first, second)
    assert order == ["first-in", "first-out", "second-in"]


# ---------------------------------------------------------------------------
# 4. Provider health reports capacity groups correctly
# ---------------------------------------------------------------------------


def test_provider_health_reports_capacity_group_quarantine():
    """A group quarantine must mark the providers behind it as not ready."""
    from tusker_gateway.provider_health import provider_health_payload

    out = provider_health_payload(
        cooldowns={
            "models": [],
            "providers": [],
            "groups": [{"group": "cli", "seconds_remaining": 120.0}],
        },
        capacity={},
        usage_groups={},
        providers={"opencode-cli", "claude-code-cli"},
        now=1_700_000_000.0,
    )

    rows = {row["provider"]: row for row in out["providers"]}
    assert rows["opencode-cli"]["ready"] is False
    assert rows["claude-code-cli"]["ready"] is False
    assert rows["opencode-cli"]["capacity_groups"] == ["cli"]
    groups = {row["group"]: row for row in out["capacity_groups"]}
    assert groups["cli"]["ready"] is False
    assert groups["cli"]["cooling_routes"] == 1


def test_provider_health_does_not_report_groups_as_providers():
    """A shared capacity group is not a provider row.

    ``opencode-cli`` and ``claude-code-cli`` share the ``cli`` budget, so the
    usage ledger records them under ``cli``. Reporting that as a provider
    distorts the provider count.
    """
    from tusker_gateway.provider_health import provider_health_payload

    out = provider_health_payload(
        cooldowns={"models": [], "providers": [], "groups": []},
        capacity={},
        usage_groups={"cli": {"requests": 4, "successes": 3, "failures": 1}},
        providers={"opencode-cli", "claude-code-cli"},
        now=1_700_000_000.0,
    )

    provider_names = {row["provider"] for row in out["providers"]}
    assert "cli" not in provider_names
    assert out["summary"]["providers"] == 2
    groups = {row["group"]: row for row in out["capacity_groups"]}
    assert groups["cli"]["requests_today"] == 4
    assert groups["cli"]["success_rate_today"] == 0.75


# ---------------------------------------------------------------------------
# 5. Retention pruning runs independently of the qualification switches
# ---------------------------------------------------------------------------


def test_retention_task_starts_without_qualification_maintenance():
    """Retention must be scheduled even when qualification maintenance is off.

    The retention window is a state-hygiene guarantee, not a probe.
    """
    source = (REPO / "tusker_gateway" / "app.py").read_text()
    assert "retention_maintenance_loop" in source
    assert '"retention_task"' in source

    maintenance = (REPO / "tusker_gateway" / "maintenance.py").read_text()
    assert "async def retention_maintenance_loop" in maintenance


@pytest.mark.asyncio
async def test_retention_loop_prunes_without_qualification_enabled(monkeypatch):
    """The retention loop prunes on its own schedule."""
    from tusker_gateway import maintenance as maintenance_module

    calls: list[int] = []

    def _fake_prune() -> dict[str, int]:
        calls.append(1)
        return {"turn_captures": 1, "audit_events": 2}

    monkeypatch.setattr(
        "tusker_gateway.retention.run_retention_prune", _fake_prune, raising=True
    )
    monkeypatch.setenv("TUSKER_RETENTION_PRUNE_INITIAL_DELAY_SECS", "0")
    monkeypatch.setenv("TUSKER_RETENTION_PRUNE_INTERVAL_SECS", "60")

    stop_event = asyncio.Event()
    task = asyncio.create_task(maintenance_module.retention_maintenance_loop(stop_event))
    for _ in range(50):
        await asyncio.sleep(0.02)
        if calls:
            break
    stop_event.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    assert calls, "retention prune never ran"


# ---------------------------------------------------------------------------
# 6. The backup manifest is in the deploy path and fails closed
# ---------------------------------------------------------------------------


def test_deploy_script_applies_the_logical_backup_manifest():
    deploy = (REPO / "k8s" / "deploy.sh").read_text()
    assert "apply -f k8s/postgres-backup.yaml" in deploy


def test_backup_command_fails_closed_when_pg_dump_fails():
    """A failed pg_dump must not publish a truncated dump as a success."""
    manifest = (REPO / "k8s" / "postgres-backup.yaml").read_text()
    assert "set -o pipefail" in manifest
