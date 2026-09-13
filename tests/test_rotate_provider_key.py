"""Smoke tests for the rotate_provider_key helper.

The helper's primary purpose is to encrypt + persist a new provider key and
bump the config-store generation, which only makes sense against the live
PostgreSQL state store (using pgcrypto). These tests cover the offline-safe
parts: import surface, CLI argument validation, and refuse-to-rotate-on-empty
behavior.
"""
import subprocess
import sys
from pathlib import Path


def test_module_imports():
    """The module exposes rotate() and main() and can be imported without psycopg."""
    from tusker_gateway.tools import rotate_provider_key

    assert callable(rotate_provider_key.rotate)
    assert callable(rotate_provider_key.main)


def test_cli_help_exits_zero():
    """`--help` is the only subcommand that must succeed without a DB."""
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "tusker_gateway.tools.rotate_provider_key", "--help"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert result.returncode == 0
    assert "provider" in result.stdout.lower()
    assert "new_key" in result.stdout.lower()


def test_rotate_without_dsn_exits():
    """Without TUSKER_STATE_DATABASE_URL the script fails fast."""
    from tusker_gateway.tools import rotate_provider_key

    import os
    env = {k: v for k, v in os.environ.items() if k != "TUSKER_STATE_DATABASE_URL"}
    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tusker_gateway.tools.rotate_provider_key",
            "google",
            "AIzaSyTESTFAKEKEY1234abcd",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
    )
    assert result.returncode != 0
    assert "TUSKER_STATE_DATABASE_URL" in result.stderr or "TUSKER_STATE_DATABASE_URL" in result.stdout


def test_rotate_refuses_short_key():
    """The rotate() function rejects empty/short keys before touching the DB."""
    from tusker_gateway.tools import rotate_provider_key

    import pytest
    with pytest.raises(SystemExit, match="empty/short"):
        rotate_provider_key.rotate("google", "")
    with pytest.raises(SystemExit, match="empty/short"):
        rotate_provider_key.rotate("google", "abc")
