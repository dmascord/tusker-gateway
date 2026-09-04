"""GitHub Copilot / OpenAI Codex OAuth enrollment flow.

Interactive device-code auth (RFC 8628) + token exchange + persistent storage.
Hermes-compatible auth.json format support.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import aiohttp

from tusker_gateway.copilot_constants import (
    _ACCESS_TOKEN_URL,
    COPILOT_OAUTH_CLIENT_ID,
    _DEVICE_CODE_URL,
    EDITOR_VERSION as _DEFAULT_EDITOR_VERSION,
    EXCHANGE_USER_AGENT as _EXCHANGE_USER_AGENT,
    JWT_REFRESH_MARGIN_SECONDS,
    _POLL_INTERVAL,
    _POLL_SAFETY,
    _TIMEOUT,
)

logger = logging.getLogger(__name__)



def _token_fingerprint(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _generate_credential_id() -> str:
    """Generate a short unique ID for a credential entry."""
    return uuid.uuid4().hex[:6]


def _now_iso() -> str:
    """Return current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    """Return current time in milliseconds since epoch."""
    return int(time.time() * 1000)


async def _post_json(
    session: aiohttp.ClientSession, url: str, data: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    """POST form data and parse JSON response."""
    body = urlencode(data).encode()
    async with session.post(url, data=body, headers=headers) as resp:
        return await resp.json()


async def enroll_device_code(
    *,
    output: Path | str | None = None,
    label: str | None = None,
    host: str = "github.com",
    interactive: bool = True,
) -> dict[str, Any] | None:
    """Run GitHub device-code OAuth flow and return the credential dict (Hermes-format).

    If *output* is given, the credential is appended to that JSON file.
    """
    if interactive and sys.stdout.isatty():
        print("\n🔐  GitHub Copilot / Codex enrollment\n")

    device_code_url = f"https://{host}/login/device/code"
    access_token_url = f"https://{host}/login/oauth/access_token"

    async with aiohttp.ClientSession() as session:
        data = await _post_json(
            session,
            device_code_url,
            {"client_id": COPILOT_OAUTH_CLIENT_ID, "scope": "read:user"},
            {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
        verification_uri = data.get("verification_uri", "https://github.com/login/device")
        user_code = data.get("user_code", "")
        device_code = data.get("device_code", "")
        interval = max(data.get("interval", _POLL_INTERVAL), 1)

        if not device_code or not user_code:
            logger.error("GitHub did not return a device code")
            if interactive:
                print("  ✗  GitHub did not return a device code.")
            return None

        if interactive:
            print(f"  1. Open this URL in your browser: {verification_uri}")
            print(f"  2. Enter this code:  {user_code}")
            print("  3. Waiting for authorization", end="", flush=True)

        deadline = time.monotonic() + _TIMEOUT
        raw_token: str | None = None

        while time.monotonic() < deadline:
            await asyncio.sleep(interval + _POLL_SAFETY)
            result = await _post_json(
                session,
                access_token_url,
                {
                    "client_id": COPILOT_OAUTH_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            )

            if result.get("access_token"):
                raw_token = result["access_token"]
                if interactive:
                    print("  ✓")
                break

            error = result.get("error", "")
            if error == "authorization_pending":
                if interactive:
                    print(".", end="", flush=True)
                continue
            elif error == "slow_down":
                interval += 5
                if interactive:
                    print(".", end="", flush=True)
                continue
            elif error == "expired_token":
                if interactive:
                    print("\n  ✗  Device code expired.")
                return None
            elif error == "access_denied":
                if interactive:
                    print("\n  ✗  Authorization was denied.")
                return None
            elif error:
                logger.error("Authorization error: %s", error)
                if interactive:
                    print(f"\n  ✗  Authorization failed: {error}")
                return None

        if not raw_token:
            if interactive:
                print("\n  ✗  Timed out waiting for authorization.")
            return None

        try:
            exchanged, expires_at = await _exchange_token(session, raw_token, host=host)
        except Exception as exc:
            logger.warning("Token exchange failed, using raw token: %s", exc)
            exchanged = raw_token
            expires_at = time.time() + 1800

        cred: dict[str, Any] = {
            "id": _generate_credential_id(),
            "label": label or "enrolled",
            "auth_type": "oauth",
            "priority": 0,
            "source": f"device_code:{label or 'enrolled'}",
            "access_token": exchanged,
            "refresh_token": raw_token,
            "base_url": "https://chatgpt.com/backend-api/codex",
            "expires_at_ms": int(expires_at * 1000),
            "last_refresh": _now_iso(),
            "request_count": 0,
            "provider": "openai-codex",
        }

        if output:
            _append_credential_hermes(output, cred)
            if interactive:
                print(f"  💾  Saved to {output}\n")

        return cred


async def _exchange_token(
    session: aiohttp.ClientSession,
    raw_token: str,
    *,
    host: str = "github.com",
) -> tuple[str, float]:
    """Exchange a raw GitHub token for a short-lived Copilot API token."""
    exchange_url = "https://api.github.com/copilot_internal/v2/token"
    if host not in ("github.com", "api.github.com"):
        exchange_url = f"https://{host}/copilot_internal/v2/token"

    async with session.get(
        exchange_url,
        headers={
            "Authorization": f"token {raw_token}",
            "User-Agent": _EXCHANGE_USER_AGENT,
            "Accept": "application/json",
            "Editor-Version": _DEFAULT_EDITOR_VERSION,
        },
    ) as resp:
        data = await resp.json()
        api_token = data.get("token", "")
        expires_at = float(data.get("expires_at", 0) or 0)
        if not api_token:
            raise ValueError("empty token in response")
        if not expires_at:
            expires_at = time.time() + 1800
        return api_token, expires_at


# ─── Hermes-compatible auth.json format ──────────────────────────────────────


def _is_hermes_format(data: Any) -> bool:
    """Return True if the loaded JSON matches the Hermes auth.json structure."""
    return isinstance(data, dict) and "credential_pool" in data


def _default_hermes_doc() -> dict[str, Any]:
    """Return a fresh Hermes-compatible auth.json skeleton."""
    return {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "updated_at": _now_iso(),
        "credential_pool": {},
    }


def _append_credential_hermes(path: Path | str, cred: dict[str, Any]) -> None:
    """Append a credential to the Hermes-format auth.json (creates if missing)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists():
        try:
            doc = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            doc = _default_hermes_doc()
    else:
        doc = _default_hermes_doc()

    if not _is_hermes_format(doc):
        # Convert legacy list-format to Hermes doc, preserving existing credentials.
        legacy = doc if isinstance(doc, list) else [doc] if doc else []
        doc = _default_hermes_doc()
        for old in legacy:
            provider = old.get("provider", "openai-codex")
            doc["credential_pool"].setdefault(provider, []).append(old)

    pool = doc["credential_pool"]
    provider = cred.get("provider", "openai-codex")
    pool.setdefault(provider, []).append(cred)
    doc["updated_at"] = _now_iso()

    _write_hermes_doc(p, doc)


def append_credential(cred: dict[str, Any], path: Path | str | None = None) -> None:
    """Append one credential while preserving every existing auth pool."""
    _append_credential_hermes(_resolve_path(path), cred)


def _write_hermes_doc(path: Path, doc: dict[str, Any]) -> None:
    """Atomically write a credential document with restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(doc, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _save_pool_hermes(path: Path | str, pool: dict[str, list[dict[str, Any]]]) -> None:
    """Overwrite the credential_pool block of a Hermes-format auth.json."""
    p = Path(path)
    if p.exists():
        try:
            doc = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            doc = _default_hermes_doc()
    else:
        doc = _default_hermes_doc()

    doc["credential_pool"] = pool
    doc["updated_at"] = _now_iso()
    _write_hermes_doc(p, doc)


def save_provider_auth_pool(
    provider: str,
    pool: list[dict[str, Any]],
    path: Path | str | None = None,
) -> None:
    """Replace one provider pool while preserving every other auth pool.

    OAuth refreshers normally own only one provider's credentials.  Writing
    that subset through :func:`save_auth_file` would silently discard the
    unrelated pools in a Hermes-format document, so provider-specific
    refreshes must use this merge-preserving path.
    """
    p = Path(_resolve_path(path))
    if p.exists():
        try:
            doc = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            doc = _default_hermes_doc()
    else:
        doc = _default_hermes_doc()

    if not _is_hermes_format(doc):
        # Preserve a legacy flat file by converting its entries before
        # replacing the requested provider's pool.
        legacy = doc if isinstance(doc, list) else [doc] if doc else []
        converted: dict[str, list[dict[str, Any]]] = {}
        for old in legacy:
            if not isinstance(old, dict):
                continue
            old_provider = str(old.get("provider", "openai-codex"))
            converted.setdefault(old_provider, []).append(old)
        doc = _default_hermes_doc()
        doc["credential_pool"] = converted

    credential_pool = doc.get("credential_pool")
    if not isinstance(credential_pool, dict):
        credential_pool = {}
        doc["credential_pool"] = credential_pool
    credential_pool[str(provider)] = list(pool)
    doc["updated_at"] = _now_iso()
    _write_hermes_doc(p, doc)


def load_auth_file(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load credentials from auth.json, supporting Hermes and legacy list formats.

    Returns a flat list across all providers when a Hermes doc is found.
    """
    p = Path(_resolve_path(path))
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    if _is_hermes_format(data):
        flat: list[dict[str, Any]] = []
        for provider, creds in data.get("credential_pool", {}).items():
            for c in creds:
                c.setdefault("provider", provider)
                flat.append(c)
        return flat
    return data if isinstance(data, list) else [data] if data else []


def load_auth_file_hermes(path: Path | str | None = None) -> dict[str, Any]:
    """Load the full Hermes auth.json document."""
    p = Path(_resolve_path(path))
    if not p.exists():
        return _default_hermes_doc()
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return _default_hermes_doc()


def save_auth_file(pool: list[dict[str, Any]], path: Path | str | None = None) -> None:
    """Save a flat credential list to auth.json in Hermes format."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for c in pool:
        provider = c.get("provider", "openai-codex")
        grouped.setdefault(provider, []).append(c)
    _save_pool_hermes(_resolve_path(path), grouped)


def _resolve_path(path: Path | str | None) -> Path:
    if path:
        return Path(path)
    env = os.getenv("TUSKER_AUTH_FILE")
    if env:
        return Path(env)
    return Path.home() / ".hermes" / "auth.json"


def list_credentials(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Return all stored credentials with fingerprints."""
    pool = load_auth_file(path)
    out = []
    for i, c in enumerate(pool):
        tok = c.get("access_token") or c.get("token", "")
        expires_at_ms = c.get("expires_at_ms", 0)
        try:
            expires_at = int(float(expires_at_ms) / 1000) if expires_at_ms else 0
        except (TypeError, ValueError):
            expires_at = 0
        if not expires_at:
            try:
                expires_at = int(float(c.get("expires_at", 0) or 0))
            except (TypeError, ValueError):
                expires_at = 0
        out.append(
            {
                "index": i,
                "label": c.get("label", ""),
                "provider": c.get("provider", ""),
                "fingerprint": _token_fingerprint(tok) if tok else "",
                "host": urlparse(str(c.get("base_url", ""))).netloc,
                "expires_at": expires_at,
                "expires_at_ms": expires_at_ms,
                "auth_type": c.get("auth_type", ""),
            }
        )
    return out


def remove_credential(index: int, path: Path | str | None = None) -> bool:
    """Remove a credential by index in the flat list view.  Returns True if removed."""
    pool = load_auth_file(path)
    if not (0 <= index < len(pool)):
        return False
    target = pool.pop(index)
    save_auth_file(pool, path)
    return True


def import_from_env(env_var: str = "CODEX_CREDENTIALS", *, path: Path | str | None = None) -> int:
    """Import credentials from an environment variable into the auth file."""
    raw = os.getenv(env_var)
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    incoming = data if isinstance(data, list) else [data] if data else []
    flat: list[dict[str, Any]] = []
    for c in incoming:
        # Map from our flat shape to Hermes shape
        cred = {
            "id": c.get("id") or _generate_credential_id(),
            "label": c.get("label", ""),
            "auth_type": c.get("auth_type", "oauth"),
            "priority": c.get("priority", 0),
            "source": c.get("source", "import"),
            "access_token": c.get("token") or c.get("access_token"),
            "refresh_token": c.get("refresh_token"),
            "base_url": c.get("base_url", "https://chatgpt.com/backend-api/codex"),
            "expires_at_ms": int(float(c.get("expires_at", 0)) * 1000) if c.get("expires_at") else 0,
            "last_refresh": _now_iso(),
            "request_count": 0,
            "provider": c.get("provider", "openai-codex"),
        }
        if cred["access_token"]:
            flat.append(cred)

    if not flat:
        return 0

    existing = load_auth_file(path)
    existing.extend(flat)
    save_auth_file(existing, path)
    return len(flat)
