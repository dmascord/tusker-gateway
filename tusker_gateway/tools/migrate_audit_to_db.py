"""Import historical JSONL audit records into the database-backed chain.

The 2026-11-24 incident moved audit writes off the shared NFS JSONL file onto
the state database. This tool carries the existing chain across so verification
still covers historical requests. It refuses to run against a non-empty audit
table: imported rows keep their original sequence numbers and hashes, so they
must be the first rows in the database.

Usage::

    python -m tusker_gateway.tools.migrate_audit_to_db \
        --file /home/tusker/.hermes/audit.jsonl --dry-run
    python -m tusker_gateway.tools.migrate_audit_to_db \
        --file /home/tusker/.hermes/audit.jsonl
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from typing import Iterable

from tusker_gateway.audit import AuditConfig, AuditLogger, _GENESIS_HASH
from tusker_gateway.storage import shared_database

logger = logging.getLogger(__name__)

_INSERT = """
INSERT INTO audit_events
    (sequence, event_id, event_type, request_id, timestamp,
     chain_version, chain_mode, previous_hash, hash, payload, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _created_at(record: dict) -> float:
    stamp = str(record.get("timestamp") or "")
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (TypeError, ValueError):
        return time.time()


def _records(path: Path) -> Iterable[tuple[int, dict]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"line {line_number} is not a JSON object")
            yield line_number, record


def migrate(*, source: Path, database_path: str, batch_size: int = 500,
            dry_run: bool = False, hmac_key: str = "") -> tuple[int, int]:
    """Return (records_scanned, records_inserted)."""
    if not source.exists():
        raise FileNotFoundError(source)

    # Verify the source chain before touching the destination.
    file_config = AuditConfig(path=str(source), backend="file", hmac_key=hmac_key)
    valid, total = AuditLogger.verify_file(file_config)
    if not valid:
        raise ValueError("source audit chain failed verification; refusing to import")

    db = shared_database(database_path, timeout=30.0, fallback_policy="critical")
    # Create the schema without adding any events.
    AuditLogger(
        AuditConfig(backend="database", database_path=database_path, fail_closed=True)
    )
    with db.connection() as conn:
        if conn.degraded:
            raise RuntimeError("audit database is unavailable")
        row = conn.execute(
            "SELECT head_sequence FROM audit_chain_state WHERE id = 1"
        ).fetchone()
        existing = int(row[0]) if row and row[0] is not None else 0
        if existing:
            raise ValueError(
                f"audit database already has {existing} events; refusing to import "
                "historical rows out of sequence order"
            )

    scanned = 0
    inserted = 0
    batch: list[tuple] = []
    previous_hash = _GENESIS_HASH
    last_hash = _GENESIS_HASH

    def flush() -> None:
        nonlocal inserted
        if not batch:
            return
        if not dry_run:
            with db.connection() as conn:
                conn.executemany(_INSERT, batch)
        inserted += len(batch)
        batch.clear()

    with db.connection() as conn:
        for line_number, record in _records(source):
            scanned += 1
            sequence = scanned
            claimed_previous = str(record.get("previous_hash") or "")
            claimed_hash = str(record.get("hash") or "")
            if claimed_previous != previous_hash:
                raise ValueError(
                    f"source chain breaks at line {line_number}: "
                    f"expected previous_hash {previous_hash[:12]}, got {claimed_previous[:12]}"
                )
            if len(claimed_hash) != 64:
                raise ValueError(f"invalid hash at line {line_number}")
            previous_hash = claimed_hash
            last_hash = claimed_hash
            payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
            batch.append(
                (
                    sequence,
                    str(record.get("event_id") or f"migrated_{sequence:012d}"),
                    str(record.get("event_type") or "unknown"),
                    str(record.get("request_id") or ""),
                    str(record.get("timestamp") or ""),
                    int(record.get("chain_version") or 1),
                    str(record.get("chain_mode") or "sha256"),
                    claimed_previous,
                    claimed_hash,
                    payload,
                    _created_at(record),
                )
            )
            if len(batch) >= batch_size:
                flush()
        flush()
        if scanned and not dry_run:
            conn.execute(
                "UPDATE audit_chain_state SET head_hash = ?, head_sequence = ? WHERE id = 1",
                (last_hash, scanned),
            )

    return scanned, inserted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="source JSONL audit log")
    parser.add_argument(
        "--database-path",
        default=os.environ.get("TUSKER_AUDIT_DATABASE_PATH", ""),
        help="logical audit DB target (resolves to the shared DSN in postgres mode)",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--hmac-key", default=os.environ.get("TUSKER_AUDIT_HMAC_KEY", "")
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_path = args.database_path or str(
        Path("/home/tusker/.hermes/audit.db")
        if Path("/home/tusker/.hermes").exists()
        else Path("/tmp/tusker-audit.db")
    )
    try:
        scanned, inserted = migrate(
            source=Path(args.file),
            database_path=database_path,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            hmac_key=args.hmac_key,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"migration failed: {exc}", flush=True)
        return 1
    print(
        f"{'dry-run: ' if args.dry_run else ''}scanned={scanned} "
        f"inserted={inserted} source={args.file}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
