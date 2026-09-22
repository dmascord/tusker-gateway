"""Enroll a Codex OAuth credential directly into the gateway's config store.

Runs OpenAI's device-code flow from inside the gateway process and writes the
resulting credential straight into ``tusker_config_oauth_credentials`` — the
database is the only place the credential lands.  Nothing is written to
``auth.json`` or any other file, so a credential enrolled here is owned solely
by the gateway.

Usage (inside the gateway pod, where the config DB and encryption key exist)::

    python -m tusker_gateway.tools.enroll_codex_credential --label dmascord
    python -m tusker_gateway.tools.enroll_codex_credential --slot 1
    python -m tusker_gateway.tools.enroll_codex_credential --replace-email a@b.c

Codex OAuth refresh tokens rotate on every use and OpenAI invalidates the
previous token for the whole account, so a credential pool must never hold two
entries for the same ``account_id``: refreshing one kills the other.  This tool
refuses a duplicate account unless ``--allow-duplicate-account`` is passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Any

from tusker_gateway.codex_oauth import CodexOAuthError, codex_token_profile, issue_codex_device_token
from tusker_gateway.config_store import ConfigStore, ConfigUnavailableError

logger = logging.getLogger(__name__)

PROVIDER = "openai-codex"

# OpenAI device codes stay valid for ~15 minutes after minting.  The API
# response carries no expiry field, so the tool derives the countdown locally.
CODEX_DEVICE_CODE_LIFETIME_SECONDS = 15 * 60


def _account_id(credential: dict[str, Any]) -> str:
    """Extract ChatGPT account_id, falling back to JWT extraction for legacy entries.

    Entries enrolled before account metadata was captured carry only
    ``access_token``/``id_token``; the same ChatGPT account is still
    identifiable from those JWTs, which duplicate detection needs.
    """
    value = credential.get("account_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    id_token = credential.get("id_token") or ""
    access_token = credential.get("access_token") or ""
    if id_token or access_token:
        return codex_token_profile(access_token, id_token).get("account_id", "")
    return ""


def _email(credential: dict[str, Any]) -> str:
    """Extract email, falling back to JWT extraction for legacy entries."""
    value = credential.get("email")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    id_token = credential.get("id_token") or ""
    access_token = credential.get("access_token") or ""
    if id_token or access_token:
        return codex_token_profile(access_token, id_token).get("email", "").lower()
    return ""


def _account_user_id(credential: dict[str, Any]) -> str:
    """Extract the per-user ChatGPT identity used as the pool dedup key.

    On ChatGPT Team / Business plans several logins share one workspace
    ``account_id``; only the composite ``chatgpt_account_user_id`` (or, when
    missing, ``chatgpt_user_id``) tells two logins apart.  Pool duplicate
    detection keys on this so team members can co-enroll without colliding.
    """
    value = credential.get("account_user_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    id_token = credential.get("id_token") or ""
    access_token = credential.get("access_token") or ""
    if id_token or access_token:
        profile = codex_token_profile(access_token, id_token)
        return profile.get("account_user_id") or profile.get("user_id", "")
    return ""


def _user_id(credential: dict[str, Any]) -> str:
    """Extract the per-user ChatGPT login id (without the account prefix)."""
    value = credential.get("user_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    id_token = credential.get("id_token") or ""
    access_token = credential.get("access_token") or ""
    if id_token or access_token:
        return codex_token_profile(access_token, id_token).get("user_id", "")
    return ""


    return ""


def duplicate_account_indices(
    credentials: list[dict[str, Any]],
    credential: dict[str, Any],
) -> list[int]:
    """Indices in ``credentials`` sharing ``credential``'s ChatGPT user identity.

    Two pool entries for one *login* cannot both stay usable: a refresh by
    either rotates that login's tokens and OpenAI invalidates the other.
    Different team members on the same workspace (``account_id``) carry
    different ``account_user_id``s and are intentionally allowed to coexist.
    Entries without an extractable ``account_user_id`` are treated as
    distinct, which is the safe default.
    """
    user = _account_user_id(credential)
    if not user:
        return []
    return [
        index
        for index, existing in enumerate(credentials)
        if _account_user_id(existing) == user
    ]


def select_slot(
    credentials: list[dict[str, Any]],
    credential: dict[str, Any],
    *,
    slot: int | None = None,
    replace_email: str | None = None,
    replace_token: str | None = None,
) -> int:
    """Return the index ``credential`` should occupy in ``credentials``.

    An explicit ``--slot`` wins and is validated against the pool.  Otherwise a
    matching ``--replace-email`` (or the credential's own email) reuses that
    entry's slot; a matching refresh token does too.  With no match the
    credential is appended.
    """
    count = len(credentials)
    if slot is not None:
        if slot < 0 or slot >= count:
            raise ValueError(
                f"--slot {slot} is out of range; pool holds {count} credential(s)"
            )
        return slot

    wanted = (replace_email or "").strip().lower() or _email(credential)
    if wanted:
        for index, existing in enumerate(credentials):
            if _email(existing) == wanted:
                return index

    if replace_token:
        for index, existing in enumerate(credentials):
            if existing.get("refresh_token") == replace_token:
                return index

    return count


def collapse_duplicates(
    credentials: list[dict[str, Any]],
    credential: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[int]]:
    """Reduce the pool to one entry per ChatGPT account, honouring ``credential``.

    Every entry sharing ``credential``'s account is removed and ``credential``
    takes over the lowest removed slot (keeping its ``id`` and ``priority``
    for log continuity).  Without a match the credential is appended.
    Returns ``(updated_pool, removed_indices)``.
    """
    matching = duplicate_account_indices(credentials, credential)
    if not matching:
        return [*credentials, credential], []
    target = min(matching)
    carrier = credentials[target]
    credential = {
        **credential,
        "id": carrier.get("id", credential.get("id")),
        "priority": carrier.get("priority", credential.get("priority", 0)),
    }
    removed = sorted(matching)
    remaining = [
        entry
        for index, entry in enumerate(credentials)
        if index not in matching
    ]
    insert_at = min(target, len(remaining))
    return remaining[:insert_at] + [credential] + remaining[insert_at:], removed


def _load_credentials(store: ConfigStore, provider: str) -> list[dict[str, Any]]:
    """Read the stored credential list for ``provider`` (empty when absent)."""
    with store._conn as conn:  # noqa: SLF001 - tool-level DB access, mirrors migrate_config_to_db
        cursor = conn.execute(
            "SELECT credentials FROM tusker_config_oauth_credentials "
            "WHERE provider = ?",
            (provider.lower(),),
        )
        row = cursor.fetchone()
        if row is None or not row[0]:
            return []
        decoded = json.loads(store._decrypt(conn, row[0]))  # noqa: SLF001
        return decoded if isinstance(decoded, list) else []


def write_credentials(
    store: ConfigStore,
    provider: str,
    credentials: list[dict[str, Any]],
) -> None:
    """Replace the provider's credential list, then hot-reload the runtime."""
    with store._conn as conn:  # noqa: SLF001
        encrypted = store._encrypt(conn, json.dumps(credentials))  # noqa: SLF001
        cursor = conn.execute(
            "UPDATE tusker_config_oauth_credentials SET credentials = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE provider = ?",
            (encrypted, provider.lower()),
        )
        if not cursor.rowcount:
            conn.execute(
                "INSERT INTO tusker_config_oauth_credentials "
                "(provider, credentials) VALUES (?, ?)",
                (provider.lower(), encrypted),
            )
        store._bump_generation(conn)  # noqa: SLF001
    store._refresh_after_write()  # noqa: SLF001


def _summarise(credentials: list[dict[str, Any]]) -> str:
    lines = []
    for index, cred in enumerate(credentials):
        user_key = _account_user_id(cred) or "(legacy)"
        account = _account_id(cred) or "?"
        label = cred.get("label") or "?"
        email = _email(cred) or "?"
        lines.append(
            f"    [{index}] label={label} email={email} "
            f"account_user_id={user_key[:20]}  account_id={account[:18]}"
        )
    return "\n".join(lines) if lines else "    (empty)"


def _run_device_flow(label: str | None, max_polls: int) -> dict[str, Any]:
    def on_authorize(url: str, user_code: str) -> None:
        minted = time.time()
        expires_at = minted + CODEX_DEVICE_CODE_LIFETIME_SECONDS
        stamp = "%Y-%m-%dT%H:%M:%SZ"
        print(
            f"\n  Open {url} and enter code: {user_code}\n"
            f"  (minted {time.strftime(stamp, time.gmtime(minted))}, "
            f"valid until {time.strftime(stamp, time.gmtime(expires_at))})\n",
            flush=True,
        )

    def on_progress(message: str) -> None:
        print(f"  {message}", flush=True)

    return asyncio.run(
        issue_codex_device_token(
            max_polls=max_polls,
            on_authorize=on_authorize,
            on_progress=on_progress,
            label=label,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="enroll_codex_credential",
        description=(
            "Enroll a Codex OAuth credential into the gateway config store "
            "via the OpenAI device-code flow."
        ),
    )
    parser.add_argument("--label", default=None, help="Label for the credential")
    parser.add_argument(
        "--slot",
        type=int,
        default=None,
        help="Replace the credential at this pool index instead of appending",
    )
    parser.add_argument(
        "--replace-email",
        default=None,
        help="Replace the credential with this email instead of appending",
    )
    parser.add_argument(
        "--allow-duplicate-account",
        action="store_true",
        help=(
            "Permit a second pool entry for an account already enrolled. "
            "Not recommended: one refresh invalidates the other entry."
        ),
    )
    parser.add_argument(
        "--collapse-account",
        action="store_true",
        help=(
            "Make this credential the pool's only entry for its ChatGPT "
            "account, dropping any other entry that shares the account"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the device flow but do not write to the config store",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=120,
        help="Maximum device authorization polls (default: 120)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    store = ConfigStore()
    if not store._managed:  # noqa: SLF001
        logger.error(
            "config store is not managed; set TUSKER_CONFIG_DATABASE_ENABLED=1 "
            "and run this inside the gateway pod"
        )
        return 1

    try:
        store._ensure_db()  # noqa: SLF001 - idempotent schema bootstrap
    except Exception as exc:  # noqa: BLE001
        logger.error("cannot initialise config store: %s", exc)
        return 1

    try:
        existing = _load_credentials(store, PROVIDER)
    except (ConfigUnavailableError, Exception) as exc:  # noqa: BLE001
        logger.error("cannot read existing credentials: %s", exc)
        return 1

    print(f"Existing {PROVIDER} credentials ({len(existing)}):")
    print(_summarise(existing))

    print("\nStarting Codex device-code enrollment…")
    try:
        credential = _run_device_flow(args.label, args.max_polls)
    except CodexOAuthError as exc:
        logger.error("device authorization failed: %s", exc)
        return 1

    email = _email(credential) or "?"
    account = _account_id(credential) or "?"
    user_key = _account_user_id(credential) or "?"
    print(
        f"\nAuthorization received: email={email} account_id={account[:18]} "
        f"account_user_id={user_key[:20]}"
    )

    if args.collapse_account:
        updated, removed = collapse_duplicates(existing, credential)
        noun = "entry" if len(removed) == 1 else "entries"
        action = (
            f"collapsed {len(removed)} duplicate {noun} for account user "
            f"{user_key[:20] or '?'}"
        )
    else:
        try:
            index = select_slot(
                existing,
                credential,
                slot=args.slot,
                replace_email=args.replace_email,
            )
        except ValueError as exc:
            logger.error("%s", exc)
            return 1

        duplicates = [
            i for i in duplicate_account_indices(existing, credential) if i != index
        ]
        if duplicates and not args.allow_duplicate_account:
            logger.error(
                "this ChatGPT login (account_user_id %s) is already enrolled at "
                "slot(s) %s; refreshing either entry invalidates the other. Pass "
                "--collapse-account to merge them, --allow-duplicate-account to "
                "override, or enroll a different ChatGPT login.",
                user_key[:20] or "?",
                duplicates,
            )
            return 1

        if index < len(existing):
            # Keep slot-stable identity and routing priority across re-enrollments.
            replaced = existing[index]
            credential = {
                **credential,
                "id": replaced.get("id", credential.get("id")),
                "priority": replaced.get("priority", credential.get("priority", 0)),
            }
            updated = list(existing)
            updated[index] = credential
            action = f"replaced slot {index}"
        else:
            updated = [*existing, credential]
            action = f"appended slot {index}"

    print(f"\nPool after enrollment ({action}):")
    print(_summarise(updated))

    if args.dry_run:
        print("\n[dry-run] config store not modified")
        return 0

    try:
        write_credentials(store, PROVIDER, updated)
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to persist credential: %s", exc)
        return 1

    print(
        f"\nPersisted to the config store (generation {store.generation}); "
        "the gateway hot-reloads it without a restart."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))