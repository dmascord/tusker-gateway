"""Regression coverage for the resilience/performance follow-up work.

Covers the four items in TODO.md (2026-11-25): scheduled retention, the
duplicate prompt estimate, cross-process OAuth refresh locking, and the
Postgres backup/HA manifests. Tests here are deterministic and offline.
"""
from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import MagicMock

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. Scheduled retention
# ---------------------------------------------------------------------------


def test_retention_prune_calls_both_stores():
    """A single prune pass must cover turn captures AND audit events."""
    from tusker_gateway.retention import run_retention_prune

    capture_store = MagicMock()
    capture_store.prune.return_value = 7
    audit = MagicMock()
    audit.prune.return_value = 3

    import tusker_gateway.turn_capture as tc

    original = tc.capture_store
    tc.capture_store = lambda: capture_store
    try:
        removed = run_retention_prune(audit=audit)
    finally:
        tc.capture_store = original

    assert removed == {"turn_captures": 7, "audit_events": 3}
    capture_store.prune.assert_called_once_with()
    audit.prune.assert_called_once_with()


def test_retention_prune_is_fail_open_independent():
    """A failure in one store must not prevent pruning the other."""
    from tusker_gateway.retention import run_retention_prune

    import tusker_gateway.turn_capture as tc

    original = tc.capture_store
    tc.capture_store = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    audit = MagicMock()
    audit.prune.return_value = 2
    try:
        removed = run_retention_prune(audit=audit)
    finally:
        tc.capture_store = original

    assert removed["turn_captures"] == 0
    assert removed["audit_events"] == 2
    audit.prune.assert_called_once_with()


def test_maintenance_loop_schedules_retention_prune():
    """The scheduler must import and call run_retention_prune."""
    source = (REPO / "tusker_gateway" / "maintenance.py").read_text()
    assert "from tusker_gateway.retention import run_retention_prune" in source
    assert "await asyncio.to_thread(run_retention_prune)" in source


def test_expired_turn_capture_pruned_without_new_capture(tmp_path, monkeypatch):
    """Expired rows are removed by prune() with no subsequent capture.

    The original prune lived only inside record(), so a traffic lull left
    expired rows forever. This asserts prune() removes them standalone.
    """
    from tusker_gateway.turn_capture import TurnCaptureConfig, TurnCaptureStore

    monkeypatch.setenv("TUSKER_STORAGE_MODE", "sqlite")
    config = TurnCaptureConfig(
        enabled=True,
        path=str(tmp_path / "captures.db"),
        ttl_secs=604_800,
        max_request_bytes=1024,
        max_response_bytes=1024,
        pending_bytes=1024,
        excluded_pools=(),
    )
    store = TurnCaptureStore(config)

    with store._db.connection() as conn:
        conn.execute(
            "INSERT INTO turn_captures "
            "(capture_id, request_id, created_at, expires_at, provider, model, pool, "
            " request_source, reason, body_policy, verdict) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "expired-1", "req-1", 0.0, 0.0, "p", "m", "code",
                "chat", "test", "full", "test",
            ),
        )

    assert store.prune() >= 1
    with store._db.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM turn_captures WHERE capture_id = ?", ("expired-1",)
        ).fetchone()
    assert row[0] == 0


# ---------------------------------------------------------------------------
# 2. Duplicate prompt estimate
# ---------------------------------------------------------------------------


def test_advisory_uses_precomputed_estimate(monkeypatch):
    """Passing prompt_estimate must not call estimate_prompt_tokens again."""
    import tusker_gateway.context_advisory as ca

    calls = []

    def counting(messages, tools=None, *a, **k):
        calls.append(1)
        return 999

    monkeypatch.setattr(ca, "estimate_prompt_tokens", counting)
    headers = ca.context_advisory_headers(
        [], None, provider="apim", model="gpt-5.6-luna", prompt_estimate=1234
    )
    assert calls == []
    assert headers["X-Tusker-Context-Estimate"] == "1234"


def test_chat_handler_passes_estimate_to_advisory():
    """Every chat response site must pass the computed estimate through."""
    source = (REPO / "tusker_gateway" / "endpoints.py").read_text()
    assert "request_prompt_estimate = estimate_prompt_tokens(" in source
    assert "responses_prompt_estimate = estimate_prompt_tokens(" in source
    call_sites = [
        line
        for line in source.splitlines()
        if "_context_advisory_headers(" in line and "def " not in line
    ]
    # 5 call sites plus 4 continuation lines carrying the estimate argument.
    assert source.count("request_prompt_estimate\n") >= 3
    assert source.count("responses_prompt_estimate\n") >= 1
    assert len(call_sites) >= 5


# ---------------------------------------------------------------------------
# 3. Cross-process OAuth refresh lock
# ---------------------------------------------------------------------------


def test_lock_key_is_stable_and_namespaced():
    from tusker_gateway.oauth_refresh_lock import _lock_key

    a = _lock_key("openai-codex", "seat-1")
    b = _lock_key("openai-codex", "seat-1")
    c = _lock_key("openai-codex", "seat-2")
    assert a == b
    assert a != c
    assert a >> 32 == 0x5455534B


@pytest.mark.asyncio
async def test_two_refresh_locks_on_same_key_are_mutually_exclusive():
    from tusker_gateway.oauth_refresh_lock import _lock_key, RefreshLock

    lock = RefreshLock()
    key = _lock_key("openai-codex", "shared-seat")
    order = []

    async def worker(name: str) -> None:
        async with lock.guard("openai-codex", "shared-seat"):
            order.append(f"{name}:enter")
            await asyncio.sleep(0.01)
            order.append(f"{name}:exit")

    await asyncio.gather(worker("a"), worker("b"))
    assert order in (
        ["a:enter", "a:exit", "b:enter", "b:exit"],
        ["b:enter", "b:exit", "a:enter", "a:exit"],
    )
    assert key  # key derivation exercised


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, sql, params=()):
        self.calls.append(sql)
        # pg_try_advisory_lock must report success so the guarded block runs;
        # every other statement just needs a no-op cursor.
        if "pg_try_advisory_lock" in sql:
            return _FakeCursor((True,))
        return _FakeCursor()


class _FakeCursor:
    def __init__(self, row=None) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class _FakeDB:
    is_postgres = True

    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def connection(self):
        conn = self._conn
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield conn

        return _ctx()


def test_config_store_advisory_lock_uses_pg_functions():
    """The lock must use pg_advisory_lock/unlock, not a plain CAS."""
    from tusker_gateway.config_store import ConfigStore

    conn = _FakeConnection()
    store = ConfigStore(database=":memory:")
    store._db = _FakeDB(conn)

    with store.oauth_refresh_advisory_lock(12345):
        pass

    assert any("pg_try_advisory_lock" in c for c in conn.calls)
    assert any("pg_advisory_unlock" in c for c in conn.calls)
    # Unlock runs even when the guarded block raises.
    conn.calls.clear()
    with pytest.raises(ValueError):
        with store.oauth_refresh_advisory_lock(12345):
            raise ValueError("refresh failed")
    assert any("pg_advisory_unlock" in c for c in conn.calls)


# ---------------------------------------------------------------------------
# 4. Backup and HA manifests
# ---------------------------------------------------------------------------


def test_postgres_backup_manifest_contains_job_and_retention():
    manifest = (REPO / "k8s" / "postgres-backup.yaml").read_text()
    assert "kind: PersistentVolumeClaim" in manifest
    assert "kind: CronJob" in manifest
    assert "pg_dump" in manifest
    assert "gzip -t" in manifest  # truncated dumps are never published
    assert "tail -n +8" in manifest  # seven-dump retention


def test_prometheus_rules_cover_postgres_and_backup():
    rules = (REPO / "k8s" / "prometheus-rules.yaml").read_text()
    for alert in (
        "TuskerGatewayPostgresNoReadyPod",
        "TuskerGatewayPostgresRestartStorm",
        "TuskerGatewayPostgresVolumeAlmostFull",
        "TuskerGatewayBackupStalled",
        "TuskerGatewayBackupNeverRan",
    ):
        assert f"alert: {alert}" in rules


def test_gateway_deployment_runs_two_replicas():
    import yaml

    manifest = yaml.safe_load((REPO / "k8s" / "deployment.yaml").read_text())
    assert manifest["spec"]["replicas"] == 2
    surge = manifest["spec"]["strategy"]["rollingUpdate"]
    assert surge["maxSurge"] == 1
    assert surge["maxUnavailable"] == 0
