"""Idempotent env-to-DB configuration migration.

Reads the env-derived gateway config (the same dict ``app["config"]`` is
built from) and inserts any section that has content into the
corresponding ConfigStore table. Rows that already exist (same primary
key) are skipped, so re-running is a no-op.

    python -m tusker_gateway.tools.migrate_config_to_db [--dry-run]

Sections migrated:
    providers          -> tusker_config_providers
    provider_api_keys  -> tusker_config_provider_api_keys (encrypted)
    pools              -> tusker_config_pools
    credential_pools   -> tusker_config_oauth_credentials (encrypted)
    api_keys           -> skipped (legacy env keys stay in env; managed
                          keys are created via POST /admin/keys)

Run against the target database with TUSKER_CONFIG_DATABASE_ENABLED=1
and the usual state DSN / encryption-key env vars set.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from tusker_gateway.config import load_config
from tusker_gateway.config_store import ConfigStore

logger = logging.getLogger("tusker_gateway.migrate_config_to_db")


def _section_counts(store: ConfigStore, config: dict, *, dry_run: bool) -> list[tuple[str, int, int, int]]:
    """Migrate each section; returns (section, inserted, skipped, errors)."""
    results: list[tuple[str, int, int, int]] = []

    # ── providers ─────────────────────────────────────────────────────
    inserted = skipped = errors = 0
    existing = {row["name"] for row in store.snapshot().get("providers", {}).values()}
    for name, provider in (config.get("providers") or {}).items():
        try:
            if name.lower() in existing:
                skipped += 1
                continue
            if not dry_run:
                store.upsert_provider({
                    "name": name,
                    "base_url": provider.base_url,
                    "chat_path": provider.chat_path,
                    "auth_env": provider.auth_env,
                    "pool_env": provider.pool_env,
                    "model_header": provider.model_header,
                    "models_path": provider.models_path,
                    "rerank_path": provider.rerank_path,
                    "model_aliases": dict(provider.model_aliases) if provider.model_aliases else None,
                    "zdr_ok": provider.zdr_ok,
                    "heavyweight": provider.heavyweight,
                })
            inserted += 1
        except Exception as exc:
            errors += 1
            logger.warning("provider %s: %s", name, exc)
    results.append(("providers", inserted, skipped, errors))

    # ── provider_api_keys ─────────────────────────────────────────────
    inserted = skipped = errors = 0
    existing = set(store.snapshot().get("provider_api_keys", {}).keys())
    for name, key in (config.get("provider_api_keys") or {}).items():
        try:
            if name.lower() in existing:
                skipped += 1
                continue
            if not dry_run:
                store.upsert_provider_credentials(name, {"api_key": key})
            inserted += 1
        except Exception as exc:
            errors += 1
            logger.warning("provider_api_key %s: %s", name, exc)
    results.append(("provider_api_keys", inserted, skipped, errors))

    # ── pools ─────────────────────────────────────────────────────────
    inserted = skipped = errors = 0
    existing = set(store.snapshot().get("pools", {}).keys())
    for name, pool in (config.get("pools") or {}).items():
        try:
            if name.lower() in existing:
                skipped += 1
                continue
            if not dry_run:
                store.upsert_pool({
                    "name": name,
                    "models": pool.models,
                    "context_window": pool.context_window,
                    "zdr": pool.zdr,
                    "provider_warmup_secs": pool.provider_warmup_secs,
                    "auto_free": pool.auto_free,
                    "heavyweight_only": pool.heavyweight_only,
                    "auto_catalog_providers": list(pool.auto_catalog_providers),
                    "fallback_pools": list(pool.fallback_pools),
                })
            inserted += 1
        except Exception as exc:
            errors += 1
            logger.warning("pool %s: %s", name, exc)
    results.append(("pools", inserted, skipped, errors))

    # ── credential_pools (OAuth) ──────────────────────────────────────
    inserted = skipped = errors = 0
    existing = set(store.snapshot().get("oauth_credentials", {}).keys())
    for name, creds in (config.get("credential_pools") or {}).items():
        try:
            if name.lower() in existing:
                skipped += 1
                continue
            if not creds:
                skipped += 1
                continue
            if not dry_run:
                with store._conn as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO tusker_config_oauth_credentials "
                        "(provider, credentials) VALUES (?, ?)",
                        (name.lower(), store._encrypt(conn, json.dumps(list(creds)))),
                    )
                    conn.execute(
                        "UPDATE tusker_config_meta SET generation = generation + 1, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = 1"
                    )
                    conn.commit()
            inserted += 1
        except Exception as exc:
            errors += 1
            logger.warning("credential_pool %s: %s", name, exc)
    results.append(("credential_pools", inserted, skipped, errors))

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be inserted without writing",
    )
    args = parser.parse_args(argv)

    if os.environ.get("TUSKER_CONFIG_DATABASE_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        print(
            "error: set TUSKER_CONFIG_DATABASE_ENABLED=1 (and the target DSN / "
            "encryption key) before migrating",
            file=sys.stderr,
        )
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config = load_config()
    store = ConfigStore(fallback_config=config)
    store.reload_now()

    counts = _section_counts(store, config, dry_run=args.dry_run)

    print("section,inserted,skipped,errors")
    total_inserted = 0
    for section, inserted, skipped, errors in counts:
        print(f"{section},{inserted},{skipped},{errors}")
        total_inserted += inserted

    if args.dry_run:
        print(f"dry run: {total_inserted} rows would be inserted")
    else:
        print(f"done: {total_inserted} rows inserted (generation {store.generation})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
