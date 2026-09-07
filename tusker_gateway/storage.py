"""Small storage abstraction for gateway state.

The gateway's original state stores are SQLite files.  SQLite remains the
default for local development and tests, but production can point the shared
state stores at PostgreSQL with ``TUSKER_STATE_DATABASE_URL``.  The adapter is
deliberately synchronous because the existing store APIs are synchronous; the
PostgreSQL connection pool keeps the short transactions bounded and shared
between store instances in one worker.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Iterator


_POSTGRES_PREFIXES = ("postgres://", "postgresql://")
_POOL_LOCK = threading.Lock()
_POOLS: dict[str, Any] = {}
_STATUS_LOCK = threading.Lock()
_STORAGE_STATUS: dict[str, Any] = {
    "configured": False,
    "backend": "sqlite",
    "degraded": False,
    "last_error": None,
    "last_error_at": None,
    "recovery_count": 0,
}
_LOGGER = logging.getLogger(__name__)


class StorageUnavailableError(RuntimeError):
    """Raised when the authoritative PostgreSQL state store is unavailable."""

    def __init__(self, message: str, *, target: str | None = None):
        super().__init__(message)
        self.target = target


def _database_connect_timeout() -> float:
    try:
        return max(
            0.1,
            float(os.environ.get("TUSKER_STATE_DATABASE_CONNECT_TIMEOUT", "2")),
        )
    except (TypeError, ValueError):
        return 2.0


def state_degraded_mode(env: dict[str, str] | None = None) -> str:
    """Return the configured unavailable-state policy."""
    values = os.environ if env is None else env
    mode = values.get("TUSKER_STATE_DEGRADED_MODE", "advisory").strip().lower()
    return mode if mode in {"advisory", "critical"} else "advisory"


def _postgres_failure_types() -> tuple[type[BaseException], ...]:
    """Return connection/pool failures that should enter degraded mode."""
    failures: list[type[BaseException]] = [OSError, TimeoutError]
    try:
        import psycopg

        failures.extend((psycopg.OperationalError, psycopg.InterfaceError))
    except ImportError:
        pass
    try:
        from psycopg_pool import PoolTimeout

        failures.append(PoolTimeout)
    except ImportError:
        pass
    return tuple(dict.fromkeys(failures))


def _mark_storage_degraded(exc: BaseException) -> None:
    now = time.time()
    with _STATUS_LOCK:
        was_degraded = bool(_STORAGE_STATUS["degraded"])
        _STORAGE_STATUS.update(
            {
                "configured": True,
                "backend": "degraded",
                "degraded": True,
                "last_error": type(exc).__name__,
                "last_error_at": datetime.fromtimestamp(
                    now, tz=timezone.utc
                ).isoformat(),
            }
        )
    if not was_degraded:
        _LOGGER.warning(
            "PostgreSQL state store unavailable; entering in-memory degraded mode: %s",
            exc,
        )


def _mark_storage_recovered() -> None:
    with _STATUS_LOCK:
        was_degraded = bool(_STORAGE_STATUS["degraded"])
        _STORAGE_STATUS.update(
            {
                "configured": True,
                "backend": "postgres",
                "degraded": False,
                "last_error": None,
                "last_error_at": None,
            }
        )
        if was_degraded:
            _STORAGE_STATUS["recovery_count"] = int(
                _STORAGE_STATUS["recovery_count"]
            ) + 1
    if was_degraded:
        _LOGGER.info("PostgreSQL state store recovered")


def storage_status() -> dict[str, Any]:
    """Return safe, non-secret health information about gateway state storage."""
    with _STATUS_LOCK:
        return dict(_STORAGE_STATUS)


def state_database_url(env: dict[str, str] | None = None) -> str:
    """Return the configured shared state DSN, if any.

    ``TUSKER_DATABASE_URL`` is accepted as a compatibility alias so a
    standard Kubernetes/PostgreSQL secret can use the conventional name.
    """
    values = os.environ if env is None else env
    return (
        values.get("TUSKER_STATE_DATABASE_URL", "").strip()
        or values.get("TUSKER_DATABASE_URL", "").strip()
    )


def is_postgres_target(target: object) -> bool:
    """Return whether a storage target is a PostgreSQL URL."""
    return isinstance(target, str) and target.lower().startswith(_POSTGRES_PREFIXES)


def storage_error_types() -> tuple[type[BaseException], ...]:
    """Return database exception classes without requiring psycopg for SQLite."""
    errors: list[type[BaseException]] = [
        sqlite3.Error,
        StorageUnavailableError,
        OSError,
        TimeoutError,
    ]
    try:
        import psycopg
    except ImportError:
        return tuple(errors)
    errors.append(psycopg.Error)
    try:
        from psycopg_pool import PoolTimeout

        errors.append(PoolTimeout)
    except ImportError:
        pass
    return tuple(errors)


def shared_state_target(path: str | os.PathLike[str]) -> str:
    """Resolve a store's path to PostgreSQL when shared state is configured."""
    raw_path = os.fspath(path)
    if raw_path == ":memory:":
        return raw_path
    return state_database_url() or raw_path


def _pool_for(dsn: str) -> Any:
    """Return the process-wide PostgreSQL pool for ``dsn``."""
    with _POOL_LOCK:
        pool = _POOLS.get(dsn)
        if pool is not None:
            return pool
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - exercised in deployment
            raise RuntimeError(
                "PostgreSQL state is configured but psycopg_pool is not installed"
            ) from exc

        try:
            max_size = max(
                2,
                int(os.environ.get("TUSKER_STATE_DATABASE_POOL_MAX", "12")),
            )
        except (TypeError, ValueError):
            max_size = 12
        pool = ConnectionPool(
            conninfo=dsn,
            # Do not synchronously open a connection here.  App construction
            # must remain possible during a PostgreSQL restart or outage.
            min_size=0,
            max_size=max_size,
            timeout=_database_connect_timeout(),
            kwargs={"application_name": "tusker-gateway"},
            open=True,
        )
        _POOLS[dsn] = pool
        return pool


class _NoopCursor:
    """Cursor-shaped result for SQLite-only PRAGMAs on PostgreSQL."""

    rowcount = 0

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []

    def __iter__(self):
        return iter(())


class _DegradedConnection:
    """A process-local, read-empty/write-no-op advisory connection.

    This is intentionally not a SQLite fallback.  It prevents an outage from
    reintroducing the old shared RWX SQLite writer while advisory stores use
    safe defaults until PostgreSQL recovers.
    """

    is_postgres = False
    degraded = True

    def execute(self, _sql: str, _parameters: Any = ()) -> _NoopCursor:
        return _NoopCursor()

    def executemany(self, _sql: str, _parameters: Any) -> _NoopCursor:
        return _NoopCursor()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _replace_qmark_placeholders(sql: str) -> str:
    """Translate SQLite qmark parameters to psycopg's ``%s`` parameters."""
    output: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'" and not in_double:
            # SQL escapes a quote inside a string by doubling it.
            if in_single and index + 1 < len(sql) and sql[index + 1] == "'":
                output.extend((char, char))
                index += 2
                continue
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        if char == "?" and not in_single and not in_double:
            output.append("%s")
        else:
            output.append(char)
        index += 1
    return "".join(output)


class DatabaseConnection:
    """Minimal connection adapter shared by the existing store classes."""

    def __init__(
        self,
        raw: Any,
        *,
        postgres: bool,
        fallback_policy: str = "advisory",
    ):
        self._raw = raw
        self.is_postgres = postgres
        self._fallback_policy = fallback_policy
        self.degraded = False

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        try:
            if self.is_postgres:
                stripped = sql.lstrip().upper()
                if stripped.startswith("PRAGMA"):
                    return _NoopCursor()
                if stripped.startswith("BEGIN IMMEDIATE"):
                    sql = "BEGIN" + sql.lstrip()[len("BEGIN IMMEDIATE"):]
                sql = _replace_qmark_placeholders(sql)
                return self._raw.execute(sql, parameters)
            return self._raw.execute(sql, parameters)
        except _postgres_failure_types() as exc:
            if self._fallback_policy == "advisory":
                _mark_storage_degraded(exc)
                self.degraded = True
                return _NoopCursor()
            raise StorageUnavailableError(
                "PostgreSQL state connection failed during query",
            ) from exc

    def executemany(self, sql: str, parameters: Any) -> Any:
        try:
            if self.is_postgres:
                sql = _replace_qmark_placeholders(sql)
                with self._raw.cursor() as cursor:
                    cursor.executemany(sql, parameters)
                    return cursor
            return self._raw.executemany(sql, parameters)
        except _postgres_failure_types() as exc:
            if self._fallback_policy == "advisory":
                _mark_storage_degraded(exc)
                self.degraded = True
                return _NoopCursor()
            raise StorageUnavailableError(
                "PostgreSQL state connection failed during batch query",
            ) from exc

    def commit(self) -> None:
        try:
            self._raw.commit()
        except _postgres_failure_types() as exc:
            if self._fallback_policy == "advisory":
                _mark_storage_degraded(exc)
                self.degraded = True
                return None
            raise StorageUnavailableError(
                "PostgreSQL state connection failed while committing",
            ) from exc

    def rollback(self) -> None:
        try:
            self._raw.rollback()
        except _postgres_failure_types() as exc:
            if self._fallback_policy == "advisory":
                _mark_storage_degraded(exc)
                self.degraded = True
                return None
            raise StorageUnavailableError(
                "PostgreSQL state connection failed while rolling back",
            ) from exc

    def close(self) -> None:
        # PostgreSQL connections are owned by the pool context.  Closing the
        # raw connection here would remove it from the pool permanently.
        if not self.is_postgres:
            self._raw.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class Database:
    """A SQLite or PostgreSQL database with a uniform context-manager API."""

    def __init__(self, target: str | os.PathLike[str], *, timeout: float = 30.0):
        self.target = os.fspath(target)
        self.timeout = timeout
        self.is_postgres = is_postgres_target(self.target)
        self.fallback_policy = state_degraded_mode()
        self._pool = None
        if self.is_postgres:
            with _STATUS_LOCK:
                _STORAGE_STATUS["configured"] = True
                _STORAGE_STATUS["backend"] = "postgres"
            try:
                self._pool = _pool_for(self.target)
            except _postgres_failure_types() as exc:
                _mark_storage_degraded(exc)
            except Exception as exc:
                # Missing psycopg_pool and malformed pool configuration are
                # also an unavailable authoritative backend, but should not
                # prevent advisory-only app construction.
                _mark_storage_degraded(exc)
        self._memory_connection: sqlite3.Connection | None = None
        if self.target == ":memory:":
            self._memory_connection = sqlite3.connect(self.target)
            self._memory_connection.execute(
                f"PRAGMA busy_timeout={max(1, int(timeout * 1000))}"
            )

    @contextmanager
    def connection(self) -> Iterator[DatabaseConnection]:
        """Yield a connection and commit or roll back the short transaction."""
        if self.is_postgres:
            yielded = False
            try:
                if self._pool is None:
                    self._pool = _pool_for(self.target)
                with self._pool.connection(
                    timeout=_database_connect_timeout()
                ) as raw:
                    connection = DatabaseConnection(
                        raw,
                        postgres=True,
                        fallback_policy=self.fallback_policy,
                    )
                    try:
                        yielded = True
                        yield connection
                    except BaseException:
                        raw.rollback()
                        raise
                    else:
                        raw.commit()
                if not connection.degraded:
                    _mark_storage_recovered()
            except StorageUnavailableError as exc:
                if yielded:
                    raise
                if self.fallback_policy == "advisory":
                    _mark_storage_degraded(exc)
                    yield _DegradedConnection()
                    return
                raise
            except _postgres_failure_types() as exc:
                _mark_storage_degraded(exc)
                if yielded and self.fallback_policy != "advisory":
                    raise StorageUnavailableError(
                        "PostgreSQL state store is unavailable",
                        target=self.target,
                    ) from exc
                if yielded:
                    return
                if self.fallback_policy == "advisory":
                    yield _DegradedConnection()
                    return
                raise StorageUnavailableError(
                    "PostgreSQL state store is unavailable",
                    target=self.target,
                ) from exc
            except Exception as exc:
                if yielded:
                    raise
                # Pool creation can fail before psycopg has a chance to expose
                # an OperationalError (for example, a missing driver). Treat
                # it the same way for graceful degraded-mode operation.
                _mark_storage_degraded(exc)
                if self.fallback_policy == "advisory":
                    yield _DegradedConnection()
                    return
                raise StorageUnavailableError(
                    "PostgreSQL state store could not provide a connection",
                    target=self.target,
                ) from exc
            return

        if self._memory_connection is not None:
            yield DatabaseConnection(
                self._memory_connection,
                postgres=False,
                fallback_policy=self.fallback_policy,
            )
            return

        raw = sqlite3.connect(self.target, timeout=self.timeout)
        raw.execute(f"PRAGMA busy_timeout={max(1, int(self.timeout * 1000))}")
        connection = DatabaseConnection(
            raw,
            postgres=False,
            fallback_policy=self.fallback_policy,
        )
        try:
            yield connection
        except BaseException:
            raw.rollback()
            raise
        else:
            raw.commit()
        finally:
            raw.close()


def shared_database(
    path: str | os.PathLike[str],
    *,
    timeout: float = 30.0,
    fallback_policy: str | None = None,
) -> Database:
    """Build a database for shared gateway state."""
    database = Database(shared_state_target(path), timeout=timeout)
    if fallback_policy is not None:
        database.fallback_policy = fallback_policy
    return database


def close_shared_pools() -> None:
    """Close process-wide PostgreSQL pools, primarily for controlled shutdowns."""
    with _POOL_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        pool.close()


__all__ = [
    "Database",
    "DatabaseConnection",
    "close_shared_pools",
    "is_postgres_target",
    "shared_database",
    "shared_state_target",
    "state_database_url",
    "state_degraded_mode",
    "storage_status",
    "storage_error_types",
    "StorageUnavailableError",
]
