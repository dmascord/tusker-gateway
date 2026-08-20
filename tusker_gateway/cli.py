"""CLI commands for managing OAuth credentials (Copilot / Codex)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from tusker_gateway.copilot_enroll import (
    enroll_device_code,
    import_from_env,
    list_credentials,
    load_auth_file,
    remove_credential,
)

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def _cmd_enroll(args: argparse.Namespace) -> int:
    async def _run() -> int:
        cred = await enroll_device_code(
            output=args.file,
            label=args.label,
            host=args.host,
            interactive=sys.stdout.isatty(),
        )
        if cred:
            print(json.dumps(cred, indent=2))
            return 0
        return 1

    return asyncio.run(_run())


def _cmd_list(args: argparse.Namespace) -> int:
    entries = list_credentials(args.file)
    if not entries:
        print("No credentials stored.")
        return 0
    print(f"{'idx':<5} {'label':<20} {'provider':<20} {'host':<20} {'fingerprint':<16} {'expires_at':<10}")
    print("-" * 100)
    for e in entries:
        print(f"{e['index']:<5} {e['label']:<20} {e['provider']:<20} {e['host']:<20} {e['fingerprint']:<16} {int(e['expires_at']):<10}")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    ok = remove_credential(args.index, args.file)
    if ok:
        print(f"Removed credential at index {args.index}.")
        return 0
    print(f"Index {args.index} is out of range.")
    return 1


def _cmd_import(args: argparse.Namespace) -> int:
    count = import_from_env(args.var, path=args.file)
    if count:
        print(f"Imported {count} credential(s) from {args.var}.")
        return 0
    print(f"No credentials found in {args.var}.")
    return 1


def _cmd_check(args: argparse.Namespace) -> int:
    pool = load_auth_file(args.file)
    ok = 0
    stale = 0
    import time

    for i, c in enumerate(pool):
        exp = float(c.get("expires_at", 0))
        is_stale = bool(exp and time.time() >= exp - 120)
        if is_stale:
            stale += 1
        else:
            ok += 1
    print(f"Credentials: {len(pool)} total ({ok} active, {stale} stale)")
    return 0 if not stale else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tusker-gateway",
        description="Manage Tusker Gateway OAuth credentials and pools.",
    )
    parser.add_argument(
        "-f", "--file",
        default=None,
        help="Auth JSON file (default: ~/.hermes/auth.json or $TUSKER_AUTH_FILE)",
    )
    sub = parser.add_subparsers(dest="command")

    # enroll
    p_enroll = sub.add_parser("enroll", help="Run GitHub device-code OAuth flow")
    p_enroll.add_argument("--label", default=None, help="Label for the credential")
    p_enroll.add_argument("--host", default="github.com", help="GitHub host (e.g. github.com or corp.ghe.com)")
    p_enroll.set_defaults(func=_cmd_enroll)

    # list
    p_list = sub.add_parser("list", help="List stored credentials")
    p_list.set_defaults(func=_cmd_list)

    # remove
    p_remove = sub.add_parser("remove", help="Remove a credential by index")
    p_remove.add_argument("index", type=int, help="Credential index")
    p_remove.set_defaults(func=_cmd_remove)

    # import
    p_import = sub.add_parser("import", help="Import credentials from an environment variable")
    p_import.add_argument("--var", default="CODEX_CREDENTIALS", help="Environment variable name")
    p_import.set_defaults(func=_cmd_import)

    # check
    p_check = sub.add_parser("check", help="Check credential health (count active/stale)")
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args(argv)
    _setup_logging()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)
