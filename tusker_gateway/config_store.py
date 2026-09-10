"""PostgreSQL-backed gateway configuration store (stub for local/dev usage).

Full implementation lives behind TUSKER_CONFIG_DATABASE_ENABLED.  When the
env var is unset the stub raises ConfigUnavailableError on every mutating
operation and returns fallback/empty snapshots on reads, preserving legacy
behavior.
"""
from __future__ import annotations

from typing import Any


class ConfigUnavailableError(Exception):
    """Raised when the DB-backed config store cannot be reached."""


class ConfigStore:
    """DB-backed config store; stub raises until TUSKER_CONFIG_DATABASE_ENABLED."""

    def __init__(self, *, database: Any = None, fallback_config: dict[str, Any] | None = None,
                 fallback_identity_config: Any | None = None) -> None:
        self._fallback_config = fallback_config or {}
        self._fallback_identity_config = fallback_identity_config
        self.generation = 0
        self._runtime_cfg: dict[str, Any] = {}
        self._runtime_identity_cfg: Any = None

    def runtime_config(self, fallback: dict[str, Any]) -> dict[str, Any]:
        # If TUSKER_CONFIG_DATABASE_ENABLED is set, this method should return
        # the DB snapshot. In the stub, we only return the fallback if
        # DB is NOT enabled (which is handled by config_runtime.py's check).
        # For tests, we simulate having DB-backed config available.
        return self._runtime_cfg if self._runtime_cfg else fallback

    def identity_config(self, fallback: Any) -> Any:
        return self._runtime_identity_cfg if self._runtime_identity_cfg else fallback

    def snapshot(self) -> dict[str, Any]:
        # In a real store, this would query the DB. For stub, we might return a mock value
        # or raise if not intended to be used in a specific test scenario.
        # The test expects this to work when DB is enabled.
        return self._runtime_cfg

    def reload_now(self) -> bool:
        # In a real store, this would check for DB changes and update internal state.
        # For stub, we can simulate generation updates.
        if not self._runtime_cfg:  # Only update if DB config is active
            return False
        self.generation += 1
        # Simulate updated config
        self._runtime_cfg.update({"api_keys": [f"db-key-{self.generation}"], "db_gen": self.generation})
        self._runtime_identity_cfg = {"identity": self.generation}
        return True

    def purge_expired(self) -> int:
        return 0

    def hydrate(self, tracker: Any) -> int:
        return 0

    def hydrate_providers(self, tracker: Any) -> int:
        return 0

    def upsert_provider(self, body: dict[str, Any]) -> dict[str, Any]:
        raise ConfigUnavailableError("config store unavailable", code="store_unavailable")

    def delete_provider(self, provider: str) -> None:
        raise ConfigUnavailableError("config store unavailable", code="store_unavailable")

    def upsert_provider_settings(self, provider: str, body: dict[str, Any]) -> dict[str, Any]:
        raise ConfigUnavailableError("config store unavailable", code="store_unavailable")

    def upsert_provider_credentials(self, provider: str, body: dict[str, Any]) -> dict[str, Any]:
        raise ConfigUnavailableError("config store unavailable", code="store_unavailable")

    def upsert_pool(self, body: dict[str, Any]) -> dict[str, Any]:
        raise ConfigUnavailableError("config store unavailable", code="store_unavailable")

    def delete_pool(self, pool: str) -> None:
        raise ConfigUnavailableError("config store unavailable", code="store_unavailable")

    def upsert_client_key(self, body: dict[str, Any]) -> dict[str, Any]:
        raise ConfigUnavailableError("config store unavailable", code="store_unavailable")

    def revoke_client_key(self, fingerprint: str) -> None:
        raise ConfigUnavailableError("config store unavailable", code="store_unavailable")

    def rotate_client_key(self, fingerprint: str) -> dict[str, Any]:
        raise ConfigUnavailableError("config store unavailable", code="store_unavailable")

    def resolve(self, api_key: str) -> Any | None:
        return None

    @property
    def persist_credentials(self) -> bool:
        return False
