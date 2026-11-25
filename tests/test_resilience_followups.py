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


@pytest.mark.asyncio
async def test_config_store_advisory_lock_uses_pg_functions():
    """The lock must use pg advisory functions and be an ASYNC context manager.

    A sync ``@contextmanager`` here raises TypeError inside ``async with``;
    the fail-open path swallows it, so the lock silently never engages. That
    shipped once (2026-11-25) and this test pins the async contract.
    """
    from tusker_gateway.config_store import ConfigStore

    conn = _FakeConnection()
    store = ConfigStore(database=":memory:")
    store._db = _FakeDB(conn)

    async with store.oauth_refresh_advisory_lock(12345):
        pass

    assert any("pg_try_advisory_lock" in c for c in conn.calls)
    assert any("pg_advisory_unlock" in c for c in conn.calls)
    # Unlock runs even when the guarded block raises.
    conn.calls.clear()
    with pytest.raises(ValueError):
        async with store.oauth_refresh_advisory_lock(12345):
            raise ValueError("refresh failed")
    assert any("pg_advisory_unlock" in c for c in conn.calls)


@pytest.mark.asyncio
async def test_refresh_lock_guard_uses_the_async_store_callback():
    """RefreshLock.guard must await the store's async context manager."""
    from tusker_gateway.oauth_refresh_lock import RefreshLock

    entered = []

    class _AsyncLock:
        async def __aenter__(self):
            entered.append("in")
            return self

        async def __aexit__(self, *exc):
            entered.append("out")
            return False

    lock = RefreshLock(acquire=lambda key: _AsyncLock())
    async with lock.guard("openai-codex", "seat-a"):
        assert entered == ["in"]
    assert entered == ["in", "out"]


@pytest.mark.asyncio
async def test_refresh_lock_guard_works_with_the_real_store_callback():
    """The guard must work with ConfigStore's real lock, not just a fake.

    Regression: the store's lock was a sync ``@contextmanager`` while the
    guard used ``async with``. The TypeError was swallowed by the fail-open
    path, so the cross-process lock silently never engaged in production
    (seen in the live startup log on 2026-11-25).
    """
    from tusker_gateway.config_store import ConfigStore
    from tusker_gateway.oauth_refresh_lock import RefreshLock

    conn = _FakeConnection()
    store = ConfigStore(database=":memory:")
    store._db = _FakeDB(conn)

    lock = RefreshLock(acquire=store.oauth_refresh_advisory_lock)
    async with lock.guard("openai-codex", "seat-a"):
        pass

    assert any("pg_try_advisory_lock" in c for c in conn.calls), (
        "the advisory lock was never acquired"
    )
    assert any("pg_advisory_unlock" in c for c in conn.calls), (
        "the advisory lock was never released"
    )


@pytest.mark.asyncio
async def test_guard_propagates_body_failure_without_rerunning_it():
    """A failed refresh must reach the caller unchanged.

    Regression: the fail-open ``except`` wrapped the guarded body too, so an
    exception from the body re-entered the body via the local-lock fallback and
    surfaced ``RuntimeError: generator didn't stop after athrow()``. Verified
    in the deployed image on 2026-11-25.
    """
    from tusker_gateway.oauth_refresh_lock import RefreshLock

    entered = 0

    class _Lock:
        async def __aenter__(self):
            nonlocal entered
            entered += 1
            return self

        async def __aexit__(self, *exc):
            return False

    lock = RefreshLock(acquire=lambda key: _Lock())
    with pytest.raises(RuntimeError, match="upstream refresh failed"):
        async with lock.guard("openai-codex", "seat-b"):
            raise RuntimeError("upstream refresh failed")
    assert entered == 1


@pytest.mark.asyncio
async def test_guard_releases_lock_when_acquire_partially_succeeds():
    """A lock acquired then failing before yield must be released.

    Session-level advisory locks survive until explicitly unlocked. If the
    store acquired then raised, the fail-open path left every other replica
    blocked on that credential.
    """
    from tusker_gateway.oauth_refresh_lock import RefreshLock

    state = {"acquired": False, "released": False}

    class _Acquire:
        async def __aenter__(self):
            state["acquired"] = True
            raise RuntimeError("db failed after acquiring")

        async def __aexit__(self, *exc):
            state["released"] = True
            return False

    lock = RefreshLock(acquire=lambda key: _Acquire())
    async with lock.guard("openai-codex", "seat-c"):
        pass

    assert state["acquired"] is True
    assert state["released"] is True, "partially acquired lock was leaked"


@pytest.mark.asyncio
async def test_guard_counts_fail_open_acquisitions():
    """A fail-open must be visible in metrics, not only in a warning log."""
    from tusker_gateway import oauth_refresh_lock as orl

    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *exc):
            return False

    class _Counter:
        def __init__(self):
            self.labels = []

        def inc(self, labels):
            self.labels.append(labels)

    class _Registry:
        def __init__(self):
            self.oauth_refresh_lock_unavailable = _Counter()

    registry = _Registry()
    original = orl._metrics_registry
    orl.bind_metrics_registry(registry)
    try:
        async with orl.RefreshLock(acquire=lambda key: _Broken()).guard("p", "id"):
            pass
    finally:
        orl.bind_metrics_registry(original)

    assert registry.oauth_refresh_lock_unavailable.labels == [{"provider": "p"}]


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
        "TuskerGatewayOAuthRefreshLockUnavailable",
    ):
        assert f"alert: {alert}" in rules


def test_gateway_deployment_runs_two_replicas():
    import yaml

    manifest = yaml.safe_load((REPO / "k8s" / "deployment.yaml").read_text())
    assert manifest["spec"]["replicas"] == 2
    surge = manifest["spec"]["strategy"]["rollingUpdate"]
    assert surge["maxSurge"] == 1
    assert surge["maxUnavailable"] == 0


def test_postgres_snapshot_job_targets_the_gateway_volume():
    """The 15m snapshot job must actually select the gateway Postgres PVC.

    Longhorn's `default` group is only implicit for volumes with no recurring
    job labels. This PVC is explicitly in the backup group, so the original
    `groups: [default]` job never snapshotted it (2026-11-25 investigation).
    """
    import yaml

    job_docs = list(
        yaml.safe_load_all((REPO / "k8s" / "postgres-snapshot-job.yaml").read_text())
    )
    job = next(d for d in job_docs if d and d.get("kind") == "RecurringJob")
    group = job["spec"]["groups"][0]
    assert group != "default"
    assert job["spec"]["task"] == "snapshot"

    pg_docs = list(
        yaml.safe_load_all((REPO / "k8s" / "gateway-postgres.yaml").read_text())
    )
    pvc = next(
        d for d in pg_docs
        if d and d.get("kind") == "PersistentVolumeClaim"
        and d["metadata"]["name"] == "tusker-gateway-postgres-data"
    )
    labels = pvc["metadata"]["labels"]
    assert labels["recurring-job.longhorn.io/source"] == "enabled"
    assert labels[f"recurring-job-group.longhorn.io/{group}"] == "enabled"
