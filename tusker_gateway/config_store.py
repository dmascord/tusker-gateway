"""PostgreSQL-backed gateway configuration store.

Full implementation behind TUSKER_CONFIG_DATABASE_ENABLED.  When the env var
is unset and no explicit database is passed, the store is inert: reads return
fallbacks and mutating methods raise ConfigUnavailableError.

Tables (all with ``tusker_config_`` prefix):
    meta                  — generation counter singleton
    providers             — provider endpoint definitions
    provider_settings     — per-provider runtime toggles
    provider_api_keys     — per-provider API keys (encrypted on PG)
    pools                 — pool model definitions
    client_keys           — managed caller identities (keys encrypted on PG)
    oauth_credentials     — OAuth credential pools (encrypted on PG)

Encryption: PostgreSQL uses pgcrypto_ with base64-encoded pgp_sym_encrypt.
SQLite stores plaintext (dev/test only; prod refuses SQLite when managed).

.. _pgcrypto: https://www.postgresql.org/docs/current/pgcrypto.html
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Callable

from tusker_gateway.config import PoolConfig, ProviderConfig, expand_env_placeholders
from tusker_gateway.identity import CallerIdentity, IdentityConfig, fingerprint_api_key
from tusker_gateway.storage import Database, shared_database


logger = logging.getLogger(__name__)

_ENCRYPTION_KEY_VARS = ("TUSKER_KEY_ENCRYPTION_KEY", "ENCRYPTION_KEY")
class ConfigUnavailableError(Exception):
    """Raised when the DB-backed config store cannot be reached."""

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class ConfigStore:
    """DB-backed config store; inert when neither DB path nor env flag is set."""

    def __init__(
        self,
        *,
        database: str | os.PathLike[str] | Database | None = None,
        fallback_config: dict[str, Any] | None = None,
        fallback_identity_config: IdentityConfig | None = None,
        _env: dict[str, str] | None = None,
    ) -> None:
        self._env = _env if _env is not None else os.environ
        self._fallback_config = fallback_config or {}
        self._fallback_identity_cfg = fallback_identity_config
        self.generation = 0
        self._runtime_cfg: dict[str, Any] = {}
        self._runtime_identity_cfg: IdentityConfig | None = None
        self._db: Database | None = None
        self._db_path: str | None = None
        self._managed = False
        self._loaded = False
        self._lock = threading.RLock()

        enabled = self._env.get("TUSKER_CONFIG_DATABASE_ENABLED", "").strip().lower()
        self._managed = enabled in {"1", "true", "yes", "on"} or database is not None

        if not self._managed:
            # Legacy mode: no DB, all ops are inert.
            return

        if database is None:
            db_path = self._env.get("TUSKER_CONFIG_DB_PATH", "").strip()
            if not db_path:
                home = self._env.get("HOME", "")
                db_path = os.path.join(home, ".hermes", "config.db") if home else "config.db"
            self._db_path = db_path
        elif isinstance(database, Database):
            self._db = database
        else:
            self._db_path = str(database)

    # ─── Lazy DB initialisation ─────────────────────────────────────────────────

    def _db_connect(self) -> Database:
        """Lazily build the shared Database wrapper on first use."""
        if self._db is not None:
            return self._db
        assert self._db_path is not None
        self._db = shared_database(self._db_path, fallback_policy="critical")
        return self._db

    @property
    def _conn(self) -> Any:
        """Thread-safe context manager for a DB connection."""
        return self._db_connect().connection()

    # ─── Schema bootstrap ───────────────────────────────────────────────────────

    def _ensure_db(self) -> None:
        """Create all tables if they do not exist. Idempotent."""
        with self._conn as conn:
            is_pg = self._db_connect().is_postgres
            if is_pg:
                conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

            for table, _ in _TABLE_SCHEMAS.items():
                conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({_TABLE_SCHEMAS[table][0]})")

            if is_pg:
                conn.execute(
                    "INSERT INTO tusker_config_meta (id, generation) VALUES (1, 0) "
                    "ON CONFLICT (id) DO NOTHING"
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO tusker_config_meta (id, generation) VALUES (1, 0)"
                )
            conn.commit()

    # ─── Encryption helpers ────────────────────────────────────────────────────

    def _encryption_key(self) -> str:
        for var in _ENCRYPTION_KEY_VARS:
            key = self._env.get(var, "").strip()
            if key:
                return key
        raise ConfigUnavailableError(
            "encryption key missing",
            code="encryption_key_missing",
        )

    def _encrypt(self, conn: Any, plaintext: str) -> str:
        """Encrypt ``plaintext`` for storage through ``conn``.

        PG: base64(pgp_sym_encrypt(plain, key)). SQLite: plaintext (dev only).
        """
        if not self._db_connect().is_postgres:
            return plaintext
        key = self._encryption_key()
        cursor = conn.execute(
            "SELECT encode(pgp_sym_encrypt(?, ?, 'cipher-algo=aes256'), 'base64')",
            (plaintext, key),
        )
        row = cursor.fetchone()
        if not row:
            raise ConfigUnavailableError("encryption failed", code="encryption_failed")
        return str(row[0])

    def _decrypt(self, conn: Any, ciphertext: str) -> str:
        """Decrypt a stored value through ``conn`` (inverse of :meth:`_encrypt`)."""
        if not self._db_connect().is_postgres:
            return ciphertext
        try:
            cursor = conn.execute(
                "SELECT pgp_sym_decrypt(decode(?, 'base64'), ?, 'cipher-algo=aes256')",
                (ciphertext, self._encryption_key()),
            )
            row = cursor.fetchone()
            if not row:
                raise ConfigUnavailableError(
                    "decryption failed", code="decryption_failed"
                )
            return str(row[0])
        except ConfigUnavailableError:
            raise
        except Exception as exc:
            raise ConfigUnavailableError(
                f"decryption failed: {exc}", code="decryption_failed"
            ) from exc

    # ─── Snapshot cache ───────────────────────────────────────────────────────

    def _apply(self, generation: int) -> None:
        """Read all DB tables and build the in-process snapshot."""
        with self._conn as conn:
            cfg = dict(self._fallback_config)
            id_cfg = (
                IdentityConfig()
                if self._fallback_identity_cfg is None
                else self._fallback_identity_cfg
            )

            is_pg = self._db_connect().is_postgres

            # ── providers ──────────────────────────────────────────────────
            providers: dict[str, ProviderConfig] = {}
            cursor = conn.execute("SELECT name, base_url, chat_path, auth_env, pool_env, "
                                 "model_header, models_path, rerank_path, model_aliases, "
                                 "zdr_ok, heavyweight FROM tusker_config_providers")
            for (name, base_url, chat_path, auth_env, pool_env, model_header,
                 models_path, rerank_path, model_aliases_raw, zdr_ok, heavyweight) in cursor:
                aliases: dict[str, str] = {}
                if model_aliases_raw:
                    try:
                        aliases = json.loads(model_aliases_raw)
                    except Exception:
                        pass
                # Infer auth kind: providers with a ``pool_env`` use credential
                # rotation (oauth/codex), so they must not be treated as bearer.
                # Only providers that carry a static ``auth_env`` are bearer-kind.
                # Unknown providers default to ``bearer`` for back-compat with the
                # legacy DEFAULT_PROVIDER_REGISTRY shape.
                if pool_env:
                    kind = "codex" if str(pool_env).startswith("opencode_codex") else "oauth"
                elif auth_env:
                    kind = "bearer"
                else:
                    kind = "bearer"
                providers[str(name).lower()] = ProviderConfig(
                    name=str(name).lower(),
                    kind=kind,
                    auth_type=kind,
                    base_url=expand_env_placeholders(str(base_url or "")) or str(base_url or ""),
                    chat_path=expand_env_placeholders(str(chat_path or "/v1/chat/completions")) or str(chat_path or "/v1/chat/completions"),
                    auth_env=str(auth_env) if auth_env else None,
                    pool_env=str(pool_env) if pool_env else None,
                    model_header=str(model_header) if model_header else None,
                    models_path=expand_env_placeholders(str(models_path) if models_path else None),
                    rerank_path=expand_env_placeholders(str(rerank_path) if rerank_path else None),
                    model_aliases=aliases,
                    zdr_ok=bool(zdr_ok),
                    heavyweight=bool(heavyweight),
                )
            if providers:
                cfg["providers"] = providers

            # ── provider_api_keys ───────────────────────────────────────────
            raw_keys: dict[str, str] = {}
            cursor = conn.execute(
                "SELECT provider, key_encrypted FROM tusker_config_provider_api_keys"
            )
            for (provider, key_encrypted) in cursor:
                if key_encrypted:
                    try:
                        raw_keys[str(provider).lower()] = self._decrypt(conn, key_encrypted)
                    except ConfigUnavailableError:
                        pass
            if raw_keys:
                cfg["provider_api_keys"] = raw_keys

            # ── pools ──────────────────────────────────────────────────────
            pools: dict[str, PoolConfig] = {}
            cursor = conn.execute(
                "SELECT name, models, context_window, zdr, provider_warmup_secs, "
                "auto_free, heavyweight_only, auto_catalog_providers, fallback_pools "
                "FROM tusker_config_pools"
            )
            for (name, models_raw, context_window, zdr, warmup, auto_free,
                 heavyweight_only, ac_providers_raw, fallback_pools_raw) in cursor:
                ac_providers: tuple[str, ...] = ()
                if ac_providers_raw:
                    try:
                        ac_providers = tuple(json.loads(ac_providers_raw))
                    except Exception:
                        pass
                fallback_pools: tuple[str, ...] = ()
                if fallback_pools_raw:
                    try:
                        fallback_pools = tuple(json.loads(fallback_pools_raw))
                    except Exception:
                        pass
                models: list[dict[str, Any]] = []
                if models_raw:
                    try:
                        models = json.loads(models_raw)
                    except Exception:
                        pass
                pools[str(name).lower()] = PoolConfig(
                    name=str(name).lower(),
                    models=models,
                    context_window=int(context_window or 128000),
                    zdr=bool(zdr),
                    provider_warmup_secs=int(warmup or 300),
                    auto_free=bool(auto_free),
                    heavyweight_only=bool(heavyweight_only),
                    auto_catalog_providers=ac_providers,
                    fallback_pools=fallback_pools,
                )
            if pools:
                cfg["pools"] = pools

            # ── credential_pools (from oauth_credentials) ───────────────────
            cred_pools: dict[str, list[dict[str, Any]]] = {}
            cursor = conn.execute(
                "SELECT provider, credentials FROM tusker_config_oauth_credentials"
            )
            for (provider, creds_encrypted) in cursor:
                if creds_encrypted:
                    try:
                        cred_pools[str(provider).lower()] = json.loads(
                            self._decrypt(conn, creds_encrypted)
                        )
                    except ConfigUnavailableError:
                        pass
            if cred_pools:
                cfg["credential_pools"] = cred_pools

            # ── client_keys → api_keys + identity ────────────────────────────
            managed_keys: list[str] = []
            identities: dict[str, CallerIdentity] = {}
            cursor = conn.execute(
                "SELECT fingerprint, principal, tenant, scopes, allowed_pools, "
                "allowed_models, allowed_providers, revoked, api_key_encrypted, "
                "api_key_last4 FROM tusker_config_client_keys"
            )
            for (fingerprint, principal, tenant, scopes_raw, allowed_pools_raw,
                 allowed_models_raw, allowed_providers_raw, revoked,
                 api_key_encrypted, api_key_last4) in cursor:
                fp = str(fingerprint).lower()
                scopes = _parse_json_array(scopes_raw)
                allowed_pools = _parse_json_array(allowed_pools_raw) or ("*",)
                allowed_models = _parse_json_array(allowed_models_raw) or ("*",)
                allowed_providers = _parse_json_array(allowed_providers_raw) or ("*",)
                if not revoked and api_key_encrypted:
                    try:
                        managed_keys.append(self._decrypt(conn, api_key_encrypted))
                    except ConfigUnavailableError:
                        pass
                if principal and tenant:
                    try:
                        identities[fp] = CallerIdentity(
                            key_fingerprint=fp,
                            principal=str(principal),
                            tenant=str(tenant),
                            scopes=tuple(scopes) if scopes else ("*",),
                            allowed_pools=tuple(allowed_pools),
                            allowed_models=tuple(allowed_models),
                            allowed_providers=tuple(allowed_providers),
                        )
                    except Exception:
                        pass

            if managed_keys:
                fallback_keys = list(self._fallback_config.get("api_keys", []))
                cfg["api_keys"] = _dedupe_preserve_order(fallback_keys + managed_keys)
            cfg["config_db_keys_authoritative"] = True

            if identities:
                id_cfg = IdentityConfig(identities=identities, required=id_cfg.required)

        with self._lock:
            self._runtime_cfg = cfg
            self._runtime_identity_cfg = id_cfg
            self.generation = generation
            self._loaded = True

    # ─── Public read API ───────────────────────────────────────────────────────

    def runtime_config(self, fallback: dict[str, Any]) -> dict[str, Any]:
        if not self._managed:
            return fallback
        if not self._loaded:
            self.reload_now()
        return self._runtime_cfg if self._runtime_cfg else fallback

    def identity_config(self, fallback: IdentityConfig) -> IdentityConfig:
        if not self._managed:
            return fallback
        if not self._loaded:
            self.reload_now()
        return self._runtime_identity_cfg if self._runtime_identity_cfg else fallback

    def snapshot(self) -> dict[str, Any]:
        """Return the current DB snapshot with credentials redacted.

        Never raises; callers expect a dict even on error.
        """
        if not self._managed:
            return _STORE_UNAVAILABLE_SHAPE
        try:
            if not self._loaded:
                self.reload_now()
            return self._build_snapshot_response()
        except Exception:
            logger.exception("snapshot failed")
            return _STORE_UNAVAILABLE_SHAPE

    def _build_snapshot_response(self) -> dict[str, Any]:
        """Build the admin GET /admin/config response payload."""
        with self._conn as conn:
            result: dict[str, Any] = {
                "generation": self.generation,
                "providers": {},
                "provider_settings": {},
                "provider_api_keys": {},
                "pools": {},
                "client_keys": [],
                "oauth_credentials": {},
            }

            # providers
            cursor = conn.execute(
                "SELECT name, base_url, chat_path, auth_env, pool_env, model_header, "
                "models_path, rerank_path, model_aliases, zdr_ok, heavyweight, "
                "created_at, updated_at FROM tusker_config_providers"
            )
            for row in cursor:
                name = str(row[0]).lower()
                result["providers"][name] = {
                    "name": name,
                    "base_url": expand_env_placeholders(str(row[1] or "")) or str(row[1] or ""),
                    "chat_path": expand_env_placeholders(str(row[2] or "/v1/chat/completions")) or str(row[2] or "/v1/chat/completions"),
                    "auth_env": str(row[3]) if row[3] else None,
                    "pool_env": str(row[4]) if row[4] else None,
                    "model_header": str(row[5]) if row[5] else None,
                    "models_path": expand_env_placeholders(str(row[6]) if row[6] else None),
                    "rerank_path": expand_env_placeholders(str(row[7]) if row[7] else None),
                    "model_aliases": _try_json(row[8]),
                    "zdr_ok": bool(row[9]),
                    "heavyweight": bool(row[10]),
                    "created_at": str(row[11]) if row[11] else None,
                    "updated_at": str(row[12]) if row[12] else None,
                }

            # provider_settings
            cursor = conn.execute(
                "SELECT provider, enabled, disabled_cause, passthrough_disabled, "
                "disabled_provider, heavyweight_only, created_at, updated_at "
                "FROM tusker_config_provider_settings"
            )
            for row in cursor:
                provider = str(row[0]).lower()
                result["provider_settings"][provider] = {
                    "enabled": bool(row[1]),
                    "disabled_cause": str(row[2]) if row[2] else None,
                    "passthrough_disabled": bool(row[3]),
                    "disabled_provider": bool(row[4]),
                    "heavyweight_only": bool(row[5]),
                    "created_at": str(row[6]) if row[6] else None,
                    "updated_at": str(row[7]) if row[7] else None,
                }

            # provider_api_keys (redacted — fingerprint + last4 only)
            cursor = conn.execute(
                "SELECT provider, fingerprint, last4, updated_at "
                "FROM tusker_config_provider_api_keys"
            )
            for row in cursor:
                result["provider_api_keys"][str(row[0]).lower()] = {
                    "fingerprint": str(row[1]),
                    "last4": str(row[2]),
                    "updated_at": str(row[3]) if row[3] else None,
                }

            # pools
            cursor = conn.execute(
                "SELECT name, models, context_window, zdr, provider_warmup_secs, "
                "auto_free, heavyweight_only, auto_catalog_providers, fallback_pools, "
                "created_at, updated_at FROM tusker_config_pools"
            )
            for row in cursor:
                name = str(row[0]).lower()
                result["pools"][name] = {
                    "name": name,
                    "models": _try_json(row[1]) or [],
                    "context_window": int(row[2] or 128000),
                    "zdr": bool(row[3]),
                    "provider_warmup_secs": int(row[4] or 300),
                    "auto_free": bool(row[5]),
                    "heavyweight_only": bool(row[6]),
                    "auto_catalog_providers": _try_json(row[7]),
                    "fallback_pools": _try_json(row[8]),
                    "created_at": str(row[9]) if row[9] else None,
                    "updated_at": str(row[10]) if row[10] else None,
                }

            # client_keys (redacted)
            cursor = conn.execute(
                "SELECT fingerprint, principal, tenant, scopes, allowed_pools, "
                "allowed_models, allowed_providers, revoked, api_key_last4, "
                "created_at, updated_at FROM tusker_config_client_keys"
            )
            for row in cursor:
                result["client_keys"].append({
                    "fingerprint": str(row[0]),
                    "principal": str(row[1]),
                    "tenant": str(row[2]),
                    "scopes": _try_json(row[3]) or [],
                    "allowed_pools": _try_json(row[4]) or ["*"],
                    "allowed_models": _try_json(row[5]) or ["*"],
                    "allowed_providers": _try_json(row[6]) or ["*"],
                    "revoked": bool(row[7]),
                    "api_key_last4": str(row[8]),
                    "created_at": str(row[9]) if row[9] else None,
                    "updated_at": str(row[10]) if row[10] else None,
                })

            # oauth_credentials (count only)
            cursor = conn.execute(
                "SELECT provider, credentials, updated_at "
                "FROM tusker_config_oauth_credentials"
            )
            for row in cursor:
                creds = []
                if row[1]:
                    try:
                        creds = json.loads(self._decrypt(conn, row[1]))
                    except Exception:
                        pass
                result["oauth_credentials"][str(row[0]).lower()] = {
                    "credential_count": len(creds),
                    "updated_at": str(row[2]) if row[2] else None,
                }

            return result

    def reload_now(self) -> bool:
        """Reload all tables from DB. Returns True if generation changed."""
        if not self._managed:
            return False
        try:
            self._ensure_db()
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_unavailable") from exc
        try:
            with self._conn as conn:
                cursor = conn.execute("SELECT generation FROM tusker_config_meta WHERE id = 1")
                row = cursor.fetchone()
                new_gen = int(row[0]) if row else 0
            if not self._loaded or new_gen != self.generation:
                self._apply(new_gen)
                return True
            return False
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_unavailable") from exc

    # ─── Public write API ─────────────────────────────────────────────────────

    def _bump_generation(self, conn: Any) -> None:
        """Increment the generation counter and commit."""
        conn.execute(
            "UPDATE tusker_config_meta SET generation = generation + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = 1"
        )
        conn.commit()

    def _refresh_after_write(self) -> None:
        """Refresh the in-memory snapshot after a successful write.

        The write is already committed, so admin changes are live without
        waiting for the poll tick. A failed refresh must not fail the write
        (the admin already succeeded); the next poll recovers.
        """
        try:
            self.reload_now()
        except Exception:
            logger.exception("post-write config refresh failed")


    # ── Provider CRUD ──────────────────────────────────────────────────────────

    def upsert_provider(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create or replace a provider definition."""
        if not self._managed:
            raise ConfigUnavailableError("config store unavailable", code="store_unavailable")
        name = str(body.get("name") or "").strip().lower()
        if not name:
            raise ValueError("provider name is required")
        self._ensure_db()
        try:
            with self._conn as conn:
                model_aliases_raw = None
                if "model_aliases" in body and body["model_aliases"]:
                    model_aliases_raw = json.dumps(body["model_aliases"])
                conn.execute(
                    "INSERT INTO tusker_config_providers "
                    "(name, base_url, chat_path, auth_env, pool_env, model_header, "
                    "models_path, rerank_path, model_aliases, zdr_ok, heavyweight) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "base_url=excluded.base_url, chat_path=excluded.chat_path, "
                    "auth_env=excluded.auth_env, pool_env=excluded.pool_env, "
                    "model_header=excluded.model_header, "
                    "models_path=excluded.models_path, "
                    "rerank_path=excluded.rerank_path, "
                    "model_aliases=excluded.model_aliases, "
                    "zdr_ok=excluded.zdr_ok, heavyweight=excluded.heavyweight, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (
                        name,
                        str(body.get("base_url") or ""),
                        str(body.get("chat_path") or "/v1/chat/completions"),
                        _null(body.get("auth_env")),
                        _null(body.get("pool_env")),
                        _null(body.get("model_header")),
                        _null(body.get("models_path")),
                        _null(body.get("rerank_path")),
                        model_aliases_raw,
                        int(bool(body.get("zdr_ok"))),
                        int(bool(body.get("heavyweight"))),
                    ),
                )
                self._bump_generation(conn)
            self._refresh_after_write()
            return {"name": name, "ok": True}
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_error") from exc

    def delete_provider(self, provider: str) -> None:
        if not self._managed:
            raise ConfigUnavailableError("config store unavailable", code="store_unavailable")
        name = str(provider).strip().lower()
        self._ensure_db()
        try:
            with self._conn as conn:
                cur = conn.execute(
                    "SELECT name FROM tusker_config_providers WHERE name = ?", (name,)
                )
                if cur.fetchone() is None:
                    raise KeyError(f"provider not found: {name}")
                conn.execute("DELETE FROM tusker_config_providers WHERE name = ?", (name,))
                self._bump_generation(conn)
            self._refresh_after_write()
        except KeyError:
            raise
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_error") from exc

    # ── Provider settings ─────────────────────────────────────────────────────

    def upsert_provider_settings(self, provider: str, body: dict[str, Any]) -> dict[str, Any]:
        """Update per-provider runtime toggles."""
        if not self._managed:
            raise ConfigUnavailableError("config store unavailable", code="store_unavailable")
        name = str(provider).strip().lower()
        self._ensure_db()
        try:
            with self._conn as conn:
                conn.execute(
                    "INSERT INTO tusker_config_provider_settings "
                    "(provider, enabled, disabled_cause, passthrough_disabled, "
                    "disabled_provider, heavyweight_only) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (provider) DO UPDATE SET "
                    "enabled=excluded.enabled, "
                    "disabled_cause=excluded.disabled_cause, "
                    "passthrough_disabled=excluded.passthrough_disabled, "
                    "disabled_provider=excluded.disabled_provider, "
                    "heavyweight_only=excluded.heavyweight_only, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (
                        name,
                        int(bool(body.get("enabled", True))),
                        _null(body.get("disabled_cause")),
                        int(bool(body.get("passthrough_disabled"))),
                        int(bool(body.get("disabled_provider"))),
                        int(bool(body.get("heavyweight_only"))),
                    ),
                )
                self._bump_generation(conn)
            self._refresh_after_write()
            return {"provider": name, "ok": True}
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_error") from exc

    # ── Provider credentials (API key) ────────────────────────────────────────

    def upsert_provider_credentials(self, provider: str, body: dict[str, Any]) -> dict[str, Any]:
        """Store or replace a provider's API key."""
        if not self._managed:
            raise ConfigUnavailableError("config store unavailable", code="store_unavailable")
        name = str(provider).strip().lower()
        api_key = str(body.get("api_key") or "").strip()
        if not api_key:
            raise ValueError("api_key is required")
        self._ensure_db()
        try:
            fp = fingerprint_api_key(api_key)
            last4 = api_key[-4:]
            with self._conn as conn:
                encrypted = self._encrypt(conn, api_key)
                conn.execute(
                    "INSERT INTO tusker_config_provider_api_keys "
                    "(provider, key_encrypted, fingerprint, last4) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (provider) DO UPDATE SET "
                    "key_encrypted=excluded.key_encrypted, "
                    "fingerprint=excluded.fingerprint, "
                    "last4=excluded.last4, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (name, encrypted, fp, last4),
                )
                self._bump_generation(conn)
            self._refresh_after_write()
            return {"provider": name, "fingerprint": fp, "last4": last4, "ok": True}
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_error") from exc

    # ── Pool CRUD ──────────────────────────────────────────────────────────────

    def upsert_pool(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create or replace a pool definition."""
        if not self._managed:
            raise ConfigUnavailableError("config store unavailable", code="store_unavailable")
        name = str(body.get("name") or "").strip().lower()
        if not name:
            raise ValueError("pool name is required")
        self._ensure_db()
        try:
            models_raw = json.dumps(body.get("models") or [])
            ac_raw = json.dumps(body.get("auto_catalog_providers") or [])
            fb_raw = json.dumps(body.get("fallback_pools") or [])
            with self._conn as conn:
                conn.execute(
                    "INSERT INTO tusker_config_pools "
                    "(name, models, context_window, zdr, provider_warmup_secs, "
                    "auto_free, heavyweight_only, auto_catalog_providers, fallback_pools) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "models=excluded.models, context_window=excluded.context_window, "
                    "zdr=excluded.zdr, provider_warmup_secs=excluded.provider_warmup_secs, "
                    "auto_free=excluded.auto_free, heavyweight_only=excluded.heavyweight_only, "
                    "auto_catalog_providers=excluded.auto_catalog_providers, "
                    "fallback_pools=excluded.fallback_pools, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (
                        name,
                        models_raw,
                        int(body.get("context_window") or 128000),
                        int(bool(body.get("zdr"))),
                        int(body.get("provider_warmup_secs") or 300),
                        int(bool(body.get("auto_free"))),
                        int(bool(body.get("heavyweight_only"))),
                        ac_raw,
                        fb_raw,
                    ),
                )
                self._bump_generation(conn)
            self._refresh_after_write()
            return {"name": name, "ok": True}
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_error") from exc

    def delete_pool(self, pool: str) -> None:
        if not self._managed:
            raise ConfigUnavailableError("config store unavailable", code="store_unavailable")
        name = str(pool).strip().lower()
        self._ensure_db()
        try:
            with self._conn as conn:
                cur = conn.execute(
                    "SELECT name FROM tusker_config_pools WHERE name = ?", (name,)
                )
                if cur.fetchone() is None:
                    raise KeyError(f"pool not found: {name}")
                conn.execute("DELETE FROM tusker_config_pools WHERE name = ?", (name,))
                self._bump_generation(conn)
            self._refresh_after_write()
        except KeyError:
            raise
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_error") from exc

    # ── Client key (identity) CRUD ─────────────────────────────────────────────

    def upsert_client_key(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create or update a managed identity key.

        On create (no fingerprint in body): a user-provided ``api_key`` is
        honoured (fingerprint is recomputed from it); otherwise a new raw
        key is generated.  On update (fingerprint in body): profile fields
        only — supply ``api_key`` would change the credential, which is
        ``rotate_client_key``'s job, so it is rejected.
        Returns ``api_key`` only in the create response.
        """
        if not self._managed:
            raise ConfigUnavailableError("config store unavailable", code="store_unavailable")
        self._ensure_db()
        try:
            principal = str(body.get("principal") or "").strip()
            tenant = str(body.get("tenant") or "").strip()
            if not principal or not tenant:
                raise ValueError("principal and tenant are required")

            # Determine fingerprint and raw key
            provided_key = body.get("api_key")
            if isinstance(provided_key, str) and provided_key.strip():
                provided_key = provided_key.strip()
            else:
                provided_key = None

            if "fingerprint" in body:
                fp = str(body["fingerprint"]).strip().lower()
                raw_key: str | None = None
                if provided_key is not None:
                    raise ValueError(
                        "api_key is not accepted when fingerprint is provided; "
                        "use POST /admin/keys/{fp}/rotate to change the raw key"
                    )
            else:
                if provided_key is not None:
                    raw_key = provided_key
                else:
                    raw_key = "sk-" + secrets.token_hex(24)
                fp = fingerprint_api_key(raw_key)
            scopes_raw = json.dumps(_normalise_patterns(body.get("scopes")))
            pools_raw = json.dumps(_normalise_patterns(body.get("allowed_pools", ["*"])))
            models_raw = json.dumps(_normalise_patterns(body.get("allowed_models", ["*"])))
            providers_raw = json.dumps(
                _normalise_patterns(body.get("allowed_providers", ["*"]))
            )
            revoked = int(bool(body.get("revoked")))
            last4 = raw_key[-4:] if raw_key else None

            with self._conn as conn:
                if raw_key is not None:
                    encrypted = self._encrypt(conn, raw_key)
                    conn.execute(
                        "INSERT INTO tusker_config_client_keys "
                        "(fingerprint, principal, tenant, scopes, allowed_pools, "
                        "allowed_models, allowed_providers, revoked, api_key_encrypted, "
                        "api_key_last4) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (fingerprint) DO UPDATE SET "
                        "principal=excluded.principal, tenant=excluded.tenant, "
                        "scopes=excluded.scopes, allowed_pools=excluded.allowed_pools, "
                        "allowed_models=excluded.allowed_models, "
                        "allowed_providers=excluded.allowed_providers, "
                        "revoked=excluded.revoked, "
                        "api_key_encrypted=excluded.api_key_encrypted, "
                        "api_key_last4=excluded.api_key_last4, "
                        "updated_at=CURRENT_TIMESTAMP",
                        (
                            fp, principal, tenant, scopes_raw, pools_raw,
                            models_raw, providers_raw, revoked,
                            encrypted, last4,
                        ),
                    )
                else:
                    # Profile-only update: preserve the stored raw key.
                    existing = conn.execute(
                        "SELECT api_key_encrypted, api_key_last4 "
                        "FROM tusker_config_client_keys WHERE fingerprint = ?",
                        (fp,),
                    ).fetchone()
                    if existing is None:
                        raise KeyError(f"key not found: {fp}")
                    encrypted, last4 = str(existing[0]), str(existing[1])
                    conn.execute(
                        "INSERT INTO tusker_config_client_keys "
                        "(fingerprint, principal, tenant, scopes, allowed_pools, "
                        "allowed_models, allowed_providers, revoked, api_key_encrypted, "
                        "api_key_last4) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (fingerprint) DO UPDATE SET "
                        "principal=excluded.principal, tenant=excluded.tenant, "
                        "scopes=excluded.scopes, allowed_pools=excluded.allowed_pools, "
                        "allowed_models=excluded.allowed_models, "
                        "allowed_providers=excluded.allowed_providers, "
                        "revoked=excluded.revoked, "
                        "updated_at=CURRENT_TIMESTAMP",
                        (
                            fp, principal, tenant, scopes_raw, pools_raw,
                            models_raw, providers_raw, revoked,
                            encrypted, last4,
                        ),
                    )
                self._bump_generation(conn)

            result: dict[str, Any] = {
                "fingerprint": fp,
                "principal": principal,
                "tenant": tenant,
            }
            if raw_key is not None:
                result["api_key"] = raw_key
            self._refresh_after_write()
            return result
        except KeyError:
            raise
        except ValueError:
            raise
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_error") from exc

    def revoke_client_key(self, fingerprint: str) -> None:
        if not self._managed:
            raise ConfigUnavailableError("config store unavailable", code="store_unavailable")
        fp = str(fingerprint).strip().lower()
        self._ensure_db()
        try:
            with self._conn as conn:
                cur = conn.execute(
                    "SELECT fingerprint FROM tusker_config_client_keys WHERE fingerprint = ?",
                    (fp,),
                )
                if cur.fetchone() is None:
                    raise KeyError(f"key not found: {fp}")
                conn.execute(
                    "UPDATE tusker_config_client_keys SET revoked = 1, "
                    "updated_at = CURRENT_TIMESTAMP WHERE fingerprint = ?",
                    (fp,),
                )
                self._bump_generation(conn)
            self._refresh_after_write()
        except KeyError:
            raise
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_error") from exc

    def rotate_client_key(self, fingerprint: str) -> dict[str, Any]:
        """Rotate a managed key: delete the old row and create a new one.

        Returns the new fingerprint and raw key.
        """
        if not self._managed:
            raise ConfigUnavailableError("config store unavailable", code="store_unavailable")
        fp = str(fingerprint).strip().lower()
        self._ensure_db()
        try:
            with self._conn as conn:
                # Fetch existing profile
                cur = conn.execute(
                    "SELECT principal, tenant, scopes, allowed_pools, "
                    "allowed_models, allowed_providers FROM tusker_config_client_keys "
                    "WHERE fingerprint = ?",
                    (fp,),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"key not found: {fp}")
                principal, tenant, scopes_raw, pools_raw, models_raw, providers_raw = row

                # Generate new key
                new_key = "sk-" + secrets.token_hex(24)
                new_fp = fingerprint_api_key(new_key)
                new_last4 = new_key[-4:]
                new_encrypted = self._encrypt(conn, new_key)

                # Insert new row, delete old
                conn.execute(
                    "INSERT INTO tusker_config_client_keys "
                    "(fingerprint, principal, tenant, scopes, allowed_pools, "
                    "allowed_models, allowed_providers, revoked, api_key_encrypted, "
                    "api_key_last4) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        new_fp, str(principal), str(tenant),
                        scopes_raw, pools_raw, models_raw, providers_raw,
                        new_encrypted, new_last4,
                    ),
                )
                conn.execute(
                    "DELETE FROM tusker_config_client_keys WHERE fingerprint = ?",
                    (fp,),
                )
                self._bump_generation(conn)
            self._refresh_after_write()
            return {
                "fingerprint": new_fp,
                "api_key": new_key,
                "principal": str(principal),
                "tenant": str(tenant),
            }
        except KeyError:
            raise
        except Exception as exc:
            raise ConfigUnavailableError(str(exc), code="db_error") from exc

    # ─── Identity resolution ───────────────────────────────────────────────────

    def resolve(self, api_key: str) -> CallerIdentity | None:
        """Look up a caller identity by raw API key fingerprint.

        Returns None when the key is not found in the managed store.
        """
        if not self._managed:
            return None
        fp = fingerprint_api_key(api_key)
        try:
            with self._conn as conn:
                cur = conn.execute(
                    "SELECT principal, tenant, scopes, allowed_pools, "
                    "allowed_models, allowed_providers, revoked "
                    "FROM tusker_config_client_keys WHERE fingerprint = ?",
                    (fp,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                principal, tenant, scopes_raw, pools_raw, models_raw, providers_raw, revoked = row
                if revoked:
                    return None
                return CallerIdentity(
                    key_fingerprint=fp,
                    principal=str(principal),
                    tenant=str(tenant),
                    scopes=tuple(_parse_json_array(scopes_raw) or ("*",)),
                    allowed_pools=tuple(_parse_json_array(pools_raw) or ("*",)),
                    allowed_models=tuple(_parse_json_array(models_raw) or ("*",)),
                    allowed_providers=tuple(_parse_json_array(providers_raw) or ("*",)),
                )
        except Exception:
            return None

    # ─── OAuth credential CAS ─────────────────────────────────────────────────

    @property
    def persist_credentials(
        self,
    ) -> Callable[[str, dict[str, Any], dict[str, Any]], bool] | None:
        """Return the OAuth credential compare-and-swap callback, or None when inert."""
        if not self._managed:
            return None
        return self._cas_persist_credentials

    def _cas_persist_credentials(
        self, provider: str, expected: dict[str, Any], replacement: dict[str, Any]
    ) -> bool:
        """CAS: replace the stored credential matching ``expected`` with ``replacement``.

        Returns False when ``expected`` is no longer in the stored credential set
        (concurrent admin replacement). The caller keeps its in-memory refreshed
        credential in that case.
        """
        name = str(provider).strip().lower()
        try:
            with self._conn as conn:
                # Read current row
                cur = conn.execute(
                    "SELECT credentials FROM tusker_config_oauth_credentials "
                    "WHERE provider = ?",
                    (name,),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                current = json.loads(self._decrypt(conn, row[0]))
                if not isinstance(current, list):
                    current = []

                # Find and replace the expected entry
                found = False
                new_list = []
                for cred in current:
                    if _cred_equal(cred, expected):
                        new_list.append(replacement)
                        found = True
                    else:
                        new_list.append(cred)

                if not found:
                    return False

                # Write back
                encrypted = self._encrypt(conn, json.dumps(new_list))
                conn.execute(
                    "UPDATE tusker_config_oauth_credentials SET "
                    "credentials = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE provider = ?",
                    (encrypted, name),
                )
                self._bump_generation(conn)
                self._refresh_after_write()
                return True
        except Exception:
            return False

    # ─── Maintenance ───────────────────────────────────────────────────────────

    def purge_expired(self) -> int:
        """Remove expired entries from OAuth credential pools. Returns count removed."""
        if not self._managed:
            return 0
        removed = 0
        now = time.time()
        try:
            with self._conn as conn:
                cur = conn.execute(
                    "SELECT provider, credentials FROM tusker_config_oauth_credentials"
                )
                for (provider, creds_encrypted) in cur:
                    if not creds_encrypted:
                        continue
                    try:
                        creds = json.loads(self._decrypt(conn, creds_encrypted))
                    except Exception:
                        continue
                    original_len = len(creds)
                    creds = [
                        c
                        for c in creds
                        if not _is_credential_expired(c, now)
                    ]
                    if len(creds) < original_len:
                        removed += original_len - len(creds)
                        enc = self._encrypt(conn, json.dumps(creds))
                        conn.execute(
                            "UPDATE tusker_config_oauth_credentials "
                            "SET credentials = ?, updated_at = CURRENT_TIMESTAMP "
                            "WHERE provider = ?",
                            (enc, str(provider)),
                        )
                if removed:
                    self._bump_generation(conn)
        except Exception:
            pass
        return removed

    def hydrate(self, tracker: Any) -> int:
        """Hydrate daemon stats from the DB. Stub — returns 0."""
        return 0

    def hydrate_providers(self, tracker: Any) -> int:
        """Hydrate provider-level stats. Stub — returns 0."""
        return 0


# ─── Module-level helpers (no state) ──────────────────────────────────────────


# ─── Table schemas ────────────────────────────────────────────────────────────

_TABLE_SCHEMAS: dict[str, tuple[str, str]] = {
    "tusker_config_meta": (
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "generation BIGINT NOT NULL DEFAULT 0, "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "id",
    ),
    "tusker_config_providers": (
        "name TEXT PRIMARY KEY, "
        "base_url TEXT NOT NULL, "
        "chat_path TEXT NOT NULL DEFAULT '/v1/chat/completions', "
        "auth_env TEXT, pool_env TEXT, model_header TEXT, "
        "models_path TEXT, rerank_path TEXT, model_aliases TEXT, "
        "zdr_ok INTEGER NOT NULL DEFAULT 0, heavyweight INTEGER NOT NULL DEFAULT 0, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "name",
    ),
    "tusker_config_provider_settings": (
        "provider TEXT PRIMARY KEY, "
        "enabled INTEGER NOT NULL DEFAULT 1, "
        "disabled_cause TEXT, "
        "passthrough_disabled INTEGER NOT NULL DEFAULT 0, "
        "disabled_provider INTEGER NOT NULL DEFAULT 0, "
        "heavyweight_only INTEGER NOT NULL DEFAULT 0, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "provider",
    ),
    "tusker_config_provider_api_keys": (
        "provider TEXT PRIMARY KEY, "
        "key_encrypted TEXT NOT NULL, "
        "fingerprint TEXT NOT NULL, last4 TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "provider",
    ),
    "tusker_config_pools": (
        "name TEXT PRIMARY KEY, "
        "models TEXT NOT NULL DEFAULT '[]', "
        "context_window INTEGER NOT NULL DEFAULT 128000, "
        "zdr INTEGER NOT NULL DEFAULT 0, "
        "provider_warmup_secs INTEGER NOT NULL DEFAULT 300, "
        "auto_free INTEGER NOT NULL DEFAULT 0, "
        "heavyweight_only INTEGER NOT NULL DEFAULT 0, "
        "auto_catalog_providers TEXT, fallback_pools TEXT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "name",
    ),
    "tusker_config_client_keys": (
        "fingerprint TEXT PRIMARY KEY, "
        "principal TEXT NOT NULL, tenant TEXT NOT NULL, "
        "scopes TEXT NOT NULL DEFAULT '[]', "
        "allowed_pools TEXT NOT NULL DEFAULT '[\"*\"]', "
        "allowed_models TEXT NOT NULL DEFAULT '[\"*\"]', "
        "allowed_providers TEXT NOT NULL DEFAULT '[\"*\"]', "
        "revoked INTEGER NOT NULL DEFAULT 0, "
        "api_key_encrypted TEXT NOT NULL, "
        "api_key_last4 TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "fingerprint",
    ),
    "tusker_config_oauth_credentials": (
        "provider TEXT PRIMARY KEY, "
        "credentials TEXT NOT NULL, "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "provider",
    ),
}

_STORE_UNAVAILABLE_SHAPE = {
    "_error": "config_store_unavailable",
    "providers": {},
    "provider_settings": {},
    "provider_api_keys": {},
    "pools": {},
    "client_keys": [],
    "oauth_credentials": {},
}


# ─── Pure helpers ─────────────────────────────────────────────────────────────


def _null(value: Any) -> str | None:
    return str(value) if value is not None else None


def _parse_json_array(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _normalise_patterns(value: Any) -> list[str]:
    """Ensure patterns is a list of non-empty strings."""
    if value is None:
        return ["*"]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ["*"]
    return [str(v).strip() for v in value if str(v).strip()]


def _try_json(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _dedupe_preserve_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _is_credential_expired(cred: dict[str, Any], now: float) -> bool:
    expires_at = cred.get("expires_at")
    if expires_at is None:
        return False
    try:
        return float(expires_at) <= now
    except (TypeError, ValueError):
        return False


def _cred_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Compare two credential dicts for equality (for CAS detection)."""
    return (
        a.get("refresh_token") == b.get("refresh_token")
        and a.get("client_id") == b.get("client_id")
    )
