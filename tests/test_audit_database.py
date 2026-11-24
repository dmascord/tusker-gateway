"""Regression coverage for the database-backed integrity-chained audit log.

The file backend's NFS ``flock`` caused the 2026-11-24 incident (stale lock EIO
failed live requests closed). The database backend serializes appends with a
``FOR UPDATE`` on a singleton chain row instead, and must preserve the same
hash-chain and fail-closed contract.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tusker_gateway.audit import (
    AuditConfig,
    AuditLogger,
    AuditWriteError,
    load_audit_config_from_env,
)


@pytest.fixture(autouse=True)
def _sqlite_mode(monkeypatch):
    monkeypatch.setenv("TUSKER_STORAGE_MODE", "sqlite")
    monkeypatch.delenv("TUSKER_STATE_DATABASE_URL", raising=False)


@pytest.fixture
def config(tmp_path):
    return AuditConfig(
        backend="database",
        database_path=str(tmp_path / "audit.db"),
        fail_closed=True,
        hmac_key="audit-secret",
        retention_days=90,
    )


async def test_db_chain_appends_and_verifies(config):
    logger = AuditLogger(config)
    results = [await logger.write({"sequence": i, "event_type": "gateway.request"}) for i in range(5)]

    assert [result["sequence"] for result in results] == [1, 2, 3, 4, 5]
    assert all(result["chain_mode"] == "hmac" for result in results)
    assert AuditLogger.verify_db(config) == (True, 5)


async def test_db_sync_and_async_writes_share_one_chain(config):
    logger = AuditLogger(config)
    first = await logger.write({"event_type": "gateway.request", "request_id": "r1"})
    result = logger.write_sync({"event_type": "tool.policy.evaluation", "request_id": "r1"})

    assert result["sequence"] == 2
    assert result["previous_hash"] == first["hash"]
    assert AuditLogger.verify_db(config) == (True, 2)


async def test_db_chain_is_serialized_under_concurrency(config):
    logger = AuditLogger(config)
    results = await asyncio.gather(
        *(logger.write({"sequence": i}) for i in range(24))
    )

    assert sorted(result["sequence"] for result in results) == list(range(1, 25))
    assert AuditLogger.verify_db(config) == (True, 24)


async def test_db_chain_detects_tampering(tmp_path, config):
    logger = AuditLogger(config)
    await logger.write({"event_type": "gateway.request"})
    await logger.write({"event_type": "tool.policy.evaluation"})

    # Flip a byte in a stored payload without recomputing its hash.
    from tusker_gateway.storage import shared_database

    with shared_database(config.database_path).connection() as conn:
        conn.execute(
            "UPDATE audit_events SET payload = ? WHERE sequence = 1",
            ('{"event_type": "tampered", "timestamp": "x"}',),
        )

    assert AuditLogger.verify_db(config) == (False, 0)


async def test_db_fail_closed_raises_on_persistent_failure(config, monkeypatch):
    logger = AuditLogger(config)

    def always_fail(_event):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(logger, "_append_db", always_fail)

    with pytest.raises(AuditWriteError):
        await logger.write({"event_type": "gateway.request"})


async def test_db_advisory_skips_on_failure(tmp_path, monkeypatch):
    config = AuditConfig(
        backend="database",
        database_path=str(tmp_path / "audit.db"),
        fail_closed=False,
    )
    logger = AuditLogger(config)

    def always_fail(_event):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(logger, "_append_db", always_fail)

    assert await logger.write({"event_type": "gateway.request"}) is None


async def test_retention_prune_keeps_chain_verifiable(config):
    logger = AuditLogger(config)
    for i in range(5):
        await logger.write({"sequence": i})

    # Age the first three rows out of the retention window.
    from tusker_gateway.storage import shared_database

    with shared_database(config.database_path).connection() as conn:
        conn.execute("UPDATE audit_events SET created_at = 0 WHERE sequence <= 3")

    assert logger.prune() == 3
    assert AuditLogger.verify_db(config) == (True, 2)


async def test_migrate_file_chain_into_database(tmp_path, config):
    source = tmp_path / "audit.jsonl"
    file_logger = AuditLogger(
        AuditConfig(path=str(source), backend="file", hmac_key="audit-secret")
    )
    for i in range(3):
        await file_logger.write({"event_type": "gateway.request", "sequence": i})

    from tusker_gateway.tools.migrate_audit_to_db import migrate

    scanned, inserted = migrate(
        source=source, database_path=config.database_path,
        hmac_key="audit-secret",
    )
    assert (scanned, inserted) == (3, 3)
    assert AuditLogger.verify_db(config) == (True, 3)

    # Importing again must refuse to scramble sequence order.
    with pytest.raises(ValueError, match="already has"):
        migrate(
            source=source, database_path=config.database_path,
            hmac_key="audit-secret",
        )


def test_env_defaults_to_database_backend():
    config = load_audit_config_from_env({})
    assert config.backend == "database"
    assert config.database_enabled is True
    assert config.retention_days == 90


def test_disabled_logger_does_not_touch_disk(tmp_path):
    logger = AuditLogger(AuditConfig(path=""))
    assert logger.config.enabled is False
    assert logger._db is None
