"""Regression coverage for fail-closed audit appends on a shared volume.

The production audit log lives on a read-write-many NFS volume. Under burst
concurrency the append path could raise ``OSError``; with
``TUSKER_AUDIT_FAIL_CLOSED=true`` that surfaced to callers as a 503. The
logger now serializes in-process appends and retries transient I/O errors.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tusker_gateway.audit import AuditConfig, AuditLogger, AuditWriteError


def _config(path, **overrides) -> AuditConfig:
    return AuditConfig(path=str(path), fail_closed=True, fsync=False, **overrides)


async def test_concurrent_appends_all_persist_and_chain_verifies(tmp_path):
    """Every concurrent append lands once, and the hash chain stays intact."""
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(_config(path))

    await asyncio.gather(*(logger.write({"sequence": index}) for index in range(32)))

    records = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    assert len(records) == 32
    assert sorted(record["sequence"] for record in records) == list(range(32))

    valid, count = AuditLogger.verify_file(_config(path))
    assert valid is True
    assert count == 32

async def test_write_sync_transient_oserror_is_retried(tmp_path, monkeypatch):
    """The sync path must survive the same transient EIO that the async path retries.

    Production: a tool-approval policy event hit ``OSError(EIO)`` on the shared
    audit volume and, with fail-closed on, aborted a healthy stream.
    """
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(_config(path))
    attempts = {"count": 0}
    original = logger._append

    def flaky_append(event):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise OSError(5, "Input/output error")
        return original(event)

    monkeypatch.setattr(logger, "_append", flaky_append)

    result = logger.write_sync({"sequence": 0})

    assert result is not None
    assert attempts["count"] == 3
    assert path.read_text().strip()


async def test_write_sync_persistent_oserror_still_fails_closed(
    tmp_path, monkeypatch
):
    """Exhausting the sync retries must still raise under fail-closed."""
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(_config(path))

    def always_fail(_event):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(logger, "_append", always_fail)

    with pytest.raises(AuditWriteError):
        logger.write_sync({"sequence": 0})


async def test_write_sync_backoff_gives_transient_eio_time_to_clear(
    tmp_path, monkeypatch
):
    """The sync path must space its retries; 4 instant attempts all hit the same EIO.

    Tool-approval audit events run in a synchronous validator where the async
    backoff is unreachable. Without a short sleep the whole retry budget was
    consumed in one millisecond, so a sub-100ms shared-volume blip still failed
    the caller closed (2026-11-24).
    """
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(_config(path))
    sleeps: list[float] = []
    monkeypatch.setattr("tusker_gateway.audit.time.sleep", sleeps.append)

    def always_fail(_event):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(logger, "_append", always_fail)

    with pytest.raises(AuditWriteError):
        logger.write_sync({"sequence": 0})

    assert len(sleeps) == 3
    assert sleeps == pytest.approx([0.02, 0.04, 0.08])


async def test_transient_oserror_is_retried_instead_of_failing_closed(
    tmp_path, monkeypatch
):
    """A transient shared-volume error must not become a client-visible 503."""
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(_config(path))
    attempts = {"count": 0}
    original = logger._append

    def flaky_append(event):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise OSError(5, "Input/output error")
        return original(event)

    monkeypatch.setattr(logger, "_append", flaky_append)

    result = await logger.write({"sequence": 0})

    assert result is not None
    assert attempts["count"] == 3
    assert path.read_text().strip()


async def test_persistent_oserror_still_fails_closed(tmp_path, monkeypatch):
    """Fail-closed semantics survive the retry: exhaustion still raises."""
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(_config(path))

    def always_fail(_event):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(logger, "_append", always_fail)

    with pytest.raises(AuditWriteError):
        await logger.write({"sequence": 0})


async def test_disabled_logger_does_not_touch_disk(tmp_path):
    """An unconfigured audit path stays a no-op."""
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(AuditConfig(path=""))

    assert await logger.write({"sequence": 0}) is None
    assert not path.exists()