"""Rotate a provider API key in the DB-backed config store.

The gateway reads provider keys from ``tusker_config_provider_api_keys``
(encrypted at rest with pgcrypto via ``TUSKER_KEY_ENCRYPTION_KEY``). Updating a
row there, then bumping ``tusker_config_meta.generation``, causes the gateway
to load the new key on its next config reload — without restarting the pod.

Usage::

    # Rotate the Google (Gemini) API key
    python -m tusker_gateway.tools.rotate_provider_key google NEW_KEY

    # Rotate any other provider
    python -m tusker_gateway.tools.rotate_provider_key openrouter sk-or-...

The new key must be supplied by the operator. This script only writes it
into the encrypted config-store row and bumps the generation counter.

Notes
-----
* Requires the ``psycopg`` Python package and a reachable PostgreSQL with the
  ``pgcrypto`` extension installed.
* Reads ``TUSKER_STATE_DATABASE_URL`` from the environment.
* Bumps ``tusker_config_meta.generation`` so the gateway reloads.
* If the provider row does not exist yet, it is created.
* Prints the new key's last4 and fingerprint for the operator's audit log.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone


def _last4(value: str) -> str:
    return value[-4:] if len(value) >= 4 else value


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encrypt(conn, plaintext: str) -> str:
    """Encrypt via pgcrypto using the gateway's TUSKER_KEY_ENCRYPTION_KEY.

    The key is stored in a Kubernetes Secret; the gateway reads it from
    ``TUSKER_KEY_ENCRYPTION_KEY``. We replicate the gateway's encryption
    pattern (pgp_sym_encrypt with the same key) so the gateway's
    pgp_sym_decrypt round-trip succeeds.
    """
    key = os.environ.get("TUSKER_KEY_ENCRYPTION_KEY", "").strip()
    if not key:
        raise SystemExit(
            "TUSKER_KEY_ENCRYPTION_KEY is not set; the gateway uses this to "
            "decrypt provider keys. Set it before running this script."
        )
    cur = conn.cursor()
    try:
        cur.execute("SELECT pgp_sym_encrypt(%s, %s)", (plaintext, key))
        row = cur.fetchone()
        if not row:
            raise SystemExit("pgp_sym_encrypt returned no value")
        return row[0]
    finally:
        cur.close()


def _connect(dsn: str):
    """Open a database connection. Imports psycopg lazily so test suites that
    substitute a sqlite-backed store can run without psycopg installed.
    """
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(
            "psycopg is required for tusker_gateway.tools.rotate_provider_key; "
            "install psycopg[binary] in the operator's runtime."
        ) from exc
    return psycopg.connect(dsn)


def rotate(provider: str, new_key: str) -> None:
    if not new_key or len(new_key.strip()) < 8:
        raise SystemExit("refusing to rotate with an empty/short key")
    dsn = os.environ.get("TUSKER_STATE_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("TUSKER_STATE_DATABASE_URL is not set")

    provider_norm = provider.strip().lower().replace("_", "-")
    last4 = _last4(new_key)
    fingerprint = _fingerprint(new_key)
    encrypted = _encrypt(_connect(dsn), new_key)

    now = datetime.now(timezone.utc)
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tusker_config_provider_api_keys
                    (provider, key_encrypted, fingerprint, last4, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider) DO UPDATE SET
                    key_encrypted = EXCLUDED.key_encrypted,
                    fingerprint = EXCLUDED.fingerprint,
                    last4 = EXCLUDED.last4,
                    updated_at = EXCLUDED.updated_at
                """,
                (provider_norm, encrypted, fingerprint, last4, now, now),
            )
            cur.execute("SELECT generation FROM tusker_config_meta")
            row = cur.fetchone()
            old_gen = row[0] if row else 0
            new_gen = old_gen + 1
            cur.execute(
                "UPDATE tusker_config_meta SET generation = %s, updated_at = %s",
                (new_gen, now),
            )
        conn.commit()
    print(
        f"rotated provider={provider_norm} last4={last4} "
        f"fingerprint={fingerprint[:16]} generation {old_gen} -> {new_gen}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", help="provider name (e.g. google, openrouter)")
    parser.add_argument("new_key", help="the new API key value")
    args = parser.parse_args(argv)
    rotate(args.provider, args.new_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
