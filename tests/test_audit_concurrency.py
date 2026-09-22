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