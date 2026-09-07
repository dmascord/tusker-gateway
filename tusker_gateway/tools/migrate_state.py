"""Migrate gateway SQLite state into the shared PostgreSQL backend.

The source databases are opened read-only.  The command is intended to run
once while the gateway is stopped or in its single-writer deployment mode:

    python -m tusker_gateway.tools.migrate_state \
        --dsn "$TUSKER_STATE_DATABASE_URL" \
        --source-dir /home/tusker/.hermes

The command is idempotent for all tables and never removes or modifies the
SQLite source files.  It initializes every state schema before copying rows so
the same image can be used for both migration and runtime.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sqlite3
from typing import Iterable

from tusker_gateway.budget import BudgetConfig, BudgetTracker
from tusker_gateway.circuit_breaker import BreakerConfig, CircuitBreaker
from tusker_gateway.idempotency import IdempotencyConfig, IdempotencyStore
from tusker_gateway.model_capability import ModelCapabilityDB
from tusker_gateway.persistent_cooldown import PersistentCooldownStore
from tusker_gateway.provider_usage import ProviderUsageDB
from tusker_gateway.quality import QualityDB
from tusker_gateway.rate_limit import RateLimitConfig, RateLimiter
from tusker_gateway.storage import Database
from tusker_gateway.tool_capability import ToolCapabilityDB

logger = logging.getLogger("tusker_gateway.migrate_state")


@dataclass(frozen=True)
class TableSpec:
    filename: str
    table: str
    columns: tuple[str, ...]
    conflict: str = "nothing"


TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        "quality_db",
        "model_quality",
        (
            "provider", "model", "quality_score", "total_calls",
            "success_calls", "failure_calls", "last_success_at",
        ),
        "model_quality",
    ),
    TableSpec(
        "quality_db",
        "model_events",
        ("id", "provider", "model", "success", "latency_ms", "created_at"),
    ),
    TableSpec(
        "capability_db",
        "model_capability",
        (
            "provider", "model", "capability", "status", "source",
            "probe_version", "http_status", "latency_ms", "failure_class",
            "checked_at",
        ),
        "model_capability",
    ),
    TableSpec(
        "tool_capability_db",
        "model_tool_capability",
        (
            "provider", "model", "probe_version", "level", "status",
            "http_status", "tool_call_count", "structured_stream",
            "arguments_valid", "arguments_match", "finish_reason",
            "unexpected_text", "latency_ms", "failure_class", "checked_at",
        ),
        "model_tool_capability",
    ),
    TableSpec(
        "provider_usage_db",
        "provider_usage_daily",
        (
            "usage_day", "group_name", "provider", "model", "requests",
            "successes", "failures", "capacity_rejections", "prompt_tokens",
            "completion_tokens", "updated_at",
        ),
        "provider_usage_daily",
    ),
    TableSpec(
        "cooldowns_db",
        "cooldowns",
        ("provider", "model", "until_epoch", "updated_at"),
        "cooldowns",
    ),
    TableSpec(
        "cooldowns_db",
        "provider_cooldowns",
        ("provider", "until_epoch", "updated_at"),
        "provider_cooldowns",
    ),
    TableSpec(
        "cooldowns_db",
        "capacity_group_cooldowns",
        ("group_name", "until_epoch", "updated_at"),
        "capacity_group_cooldowns",
    ),
    TableSpec(
        "circuit_db",
        "breakers",
        (
            "provider", "model", "state", "consecutive_failures",
            "window_failures", "window_total", "window_started_at", "opened_at",
            "cooldown_secs", "half_open_probe_inflight",
        ),
        "breakers",
    ),
    TableSpec(
        "ratelimit_db",
        "buckets",
        ("fingerprint", "tokens", "last_refill_at"),
        "buckets",
    ),
    TableSpec(
        "budget_db",
        "usage",
        ("fingerprint", "period", "period_start", "tokens"),
        "usage",
    ),
    TableSpec(
        "idempotency_db",
        "idempotency_records",
        (
            "record_key", "request_hash", "state", "response_status",
            "response_body", "content_type", "lease_token", "locked_until",
            "expires_at", "created_at", "updated_at",
        ),
        "idempotency_records",
    ),
)


def _default_capability_name(source_dir: Path) -> str:
    recovered = source_dir / "model_capability-recovered.db"
    return recovered.name if recovered.exists() else "model_capability.db"


def _source_paths(source_dir: Path, args: argparse.Namespace) -> dict[str, Path]:
    return {
        "quality_db": source_dir / args.quality_db,
        "capability_db": source_dir / args.capability_db,
        "tool_capability_db": source_dir / args.tool_capability_db,
        "provider_usage_db": source_dir / args.provider_usage_db,
        "cooldowns_db": source_dir / args.cooldowns_db,
        "circuit_db": source_dir / args.circuit_db,
        "ratelimit_db": source_dir / args.ratelimit_db,
        "budget_db": source_dir / args.budget_db,
        "idempotency_db": source_dir / args.idempotency_db,
    }


def _initialize_target(dsn: str) -> None:
    """Create all runtime state tables in the target database."""
    os.environ["TUSKER_STATE_DATABASE_URL"] = dsn
    # The paths are deliberately dummy paths: shared_database() resolves them
    # to the configured DSN, so no local SQLite files are created.
    QualityDB("/tmp/tusker-gateway-quality.db")
    ModelCapabilityDB("/tmp/tusker-gateway-capability.db")
    ToolCapabilityDB("/tmp/tusker-gateway-tool-capability.db")
    ProviderUsageDB("/tmp/tusker-gateway-provider-usage.db")
    PersistentCooldownStore(Path("/tmp/tusker-gateway-cooldowns.db"))
    CircuitBreaker(BreakerConfig(enabled=True, path="/tmp/tusker-gateway-circuit.db"))
    RateLimiter(RateLimitConfig(enabled=True, path="/tmp/tusker-gateway-ratelimit.db"))
    BudgetTracker(BudgetConfig(enabled=True, path="/tmp/tusker-gateway-budget.db"))
    IdempotencyStore(
        IdempotencyConfig(enabled=True, path="/tmp/tusker-gateway-idempotency.db")
    )


def _open_source(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _source_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _insert_sql(spec: TableSpec, columns: tuple[str, ...]) -> str:
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    if spec.conflict == "nothing":
        conflict = " ON CONFLICT DO NOTHING"
    elif spec.conflict == "model_quality":
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"provider", "model"}
        )
        conflict = (
            " ON CONFLICT(provider, model) DO UPDATE SET "
            + updates
        )
    elif spec.conflict == "model_capability":
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"provider", "model", "capability"}
        )
        conflict = (
            " ON CONFLICT(provider, model, capability) DO UPDATE SET "
            + updates
        )
    elif spec.conflict == "model_tool_capability":
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"provider", "model"}
        )
        conflict = " ON CONFLICT(provider, model) DO UPDATE SET " + updates
    elif spec.conflict == "provider_usage_daily":
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"usage_day", "group_name", "provider", "model"}
        )
        conflict = (
            " ON CONFLICT(usage_day, group_name, provider, model) "
            "DO UPDATE SET " + updates
        )
    elif spec.conflict == "cooldowns":
        updates = "until_epoch = excluded.until_epoch, updated_at = excluded.updated_at"
        conflict = " ON CONFLICT(provider, model) DO UPDATE SET " + updates
    elif spec.conflict == "provider_cooldowns":
        conflict = (
            " ON CONFLICT(provider) DO UPDATE SET "
            "until_epoch = GREATEST(provider_cooldowns.until_epoch, excluded.until_epoch), "
            "updated_at = excluded.updated_at"
        )
    elif spec.conflict == "capacity_group_cooldowns":
        conflict = (
            " ON CONFLICT(group_name) DO UPDATE SET "
            "until_epoch = GREATEST(capacity_group_cooldowns.until_epoch, excluded.until_epoch), "
            "updated_at = excluded.updated_at"
        )
    elif spec.conflict == "breakers":
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"provider", "model"}
        )
        conflict = " ON CONFLICT(provider, model) DO UPDATE SET " + updates
    elif spec.conflict == "buckets":
        conflict = (
            " ON CONFLICT(fingerprint) DO UPDATE SET "
            "tokens = excluded.tokens, last_refill_at = excluded.last_refill_at"
        )
    elif spec.conflict == "usage":
        conflict = (
            " ON CONFLICT(fingerprint, period, period_start) DO UPDATE SET "
            "tokens = excluded.tokens"
        )
    elif spec.conflict == "idempotency_records":
        updates = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column != "record_key"
        )
        conflict = " ON CONFLICT(record_key) DO UPDATE SET " + updates
    else:  # pragma: no cover - fixed table spec guard
        raise ValueError(f"unknown conflict policy: {spec.conflict}")
    return f"INSERT INTO {spec.table} ({names}) VALUES ({placeholders}){conflict}"


def _sanitize_row(row: tuple[object, ...]) -> tuple[tuple[object, ...], int]:
    """Remove SQLite-permitted NULs that PostgreSQL text rejects.

    The recovered capability database contains a small number of catalog rows
    with embedded NULs in model/capability text.  SQLite stores those bytes,
    while PostgreSQL deliberately rejects them in TEXT/VARCHAR values.  NUL
    has no useful meaning in these catalog fields, so remove it during the
    one-way migration and report how many values were normalized.
    """
    clean: list[object] = []
    changed = 0
    for value in row:
        if isinstance(value, str) and "\x00" in value:
            value = value.replace("\x00", "")
            changed += 1
        clean.append(value)
    return tuple(clean), changed


def _copy_table(
    target: Database,
    source_path: Path,
    spec: TableSpec,
    *,
    batch_size: int = 500,
) -> int:
    if not source_path.exists():
        logger.info("skip %s: source file absent", source_path)
        return 0
    with _open_source(source_path) as source:
        if not _table_exists(source, spec.table):
            logger.info("skip %s.%s: table absent", source_path.name, spec.table)
            return 0
        available = _source_columns(source, spec.table)
        columns = tuple(column for column in spec.columns if column in available)
        if not columns:
            logger.info("skip %s.%s: no compatible columns", source_path.name, spec.table)
            return 0
        query = f"SELECT {', '.join(columns)} FROM {spec.table}"
        insert_sql = _insert_sql(spec, columns)
        count = 0
        with target.connection() as connection:
            cursor = source.execute(query)
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                clean_rows: list[tuple[object, ...]] = []
                sanitized = 0
                for row in rows:
                    clean_row, changed = _sanitize_row(row)
                    clean_rows.append(clean_row)
                    sanitized += changed
                connection.executemany(insert_sql, clean_rows)
                if sanitized:
                    logger.warning(
                        "removed %d embedded NUL text values from %s.%s",
                        sanitized,
                        source_path.name,
                        spec.table,
                    )
                count += len(rows)
        logger.info("copied %d rows from %s.%s", count, source_path.name, spec.table)
        return count


def _set_event_sequence(target: Database) -> None:
    with target.connection() as connection:
        connection.execute(
            "SELECT setval(pg_get_serial_sequence('model_events', 'id'), "
            "COALESCE((SELECT MAX(id) FROM model_events), 1), true)"
        )


def migrate(args: argparse.Namespace) -> int:
    dsn = args.dsn.strip()
    if not dsn.lower().startswith(("postgres://", "postgresql://")):
        raise ValueError("--dsn must be a PostgreSQL connection URL")
    source_paths = _source_paths(Path(args.source_dir), args)
    _initialize_target(dsn)
    target = Database(dsn)
    totals: dict[str, int] = {}
    for spec in TABLE_SPECS:
        totals[spec.table] = _copy_table(target, source_paths[spec.filename], spec)
    if totals.get("model_events", 0):
        _set_event_sequence(target)
    with target.connection() as connection:
        for table in (
            "model_quality", "model_events", "model_capability",
            "model_tool_capability", "provider_usage_daily", "cooldowns",
            "provider_cooldowns", "capacity_group_cooldowns", "breakers",
            "buckets", "usage", "idempotency_records",
        ):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            logger.info("target %s rows=%d", table, count)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("TUSKER_STATE_DATABASE_URL", ""))
    parser.add_argument("--source-dir", default="/home/tusker/.hermes")
    parser.add_argument("--quality-db", default="model_quality.db")
    parser.add_argument("--capability-db", default=None)
    parser.add_argument("--tool-capability-db", default="model_tool_capability.db")
    parser.add_argument("--provider-usage-db", default="provider_usage.db")
    parser.add_argument("--cooldowns-db", default="cooldowns.db")
    parser.add_argument("--circuit-db", default="circuit.db")
    parser.add_argument("--ratelimit-db", default="ratelimit.db")
    parser.add_argument("--budget-db", default="budget.db")
    parser.add_argument("--idempotency-db", default="idempotency.db")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.capability_db:
        args.capability_db = _default_capability_name(Path(args.source_dir))
    logging.basicConfig(level=os.environ.get("TUSKER_LOG_LEVEL", "INFO"))
    try:
        return migrate(args)
    except Exception as exc:
        logger.error("state migration failed: %s", exc.__class__.__name__)
        logger.debug("state migration failure", exc_info=True)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
