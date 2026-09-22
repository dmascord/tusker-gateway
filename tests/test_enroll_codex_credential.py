"""Tests for the in-gateway Codex credential enrollment tool."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any

import pytest

from tusker_gateway.config_store import ConfigStore
from tusker_gateway.identity import IdentityConfig
from tusker_gateway.tools import enroll_codex_credential as module
from tusker_gateway.tools.enroll_codex_credential import (
    PROVIDER,
    duplicate_account_indices,
    select_slot,
    write_credentials,
)


def _make_store() -> ConfigStore:
    """Real ConfigStore against a fresh temp SQLite database."""
    dbfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    os.unlink(dbfile)
    store = ConfigStore(
        database=dbfile,
        fallback_config={},
        fallback_identity_config=IdentityConfig(),
    )
    store._ensure_db()  # noqa: SLF001 - mirrors what a running gateway does
    return store


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> ConfigStore:
    """Temp store, wired in as the store ``main()`` constructs."""
    store = _make_store()
    monkeypatch.setattr(module, "ConfigStore", lambda *a, **k: store)
    return store


def _credential(
    *,
    account_id: str = "acct-1",
    account_user_id: str | None = None,
    email: str = "[REDACTED-EMAIL]",
    refresh_token: str = "rt-1",
    label: str | None = None,
    priority: int = 0,
    cred_id: str = "cred-1",
    access_token: str = "at-1",
) -> dict[str, Any]:
    """Test credential.  ``account_user_id`` defaults to a per-login derivation
    of ``account_id`` so most tests express login identity with one field."""
    credential = {
        "id": cred_id,
        "label": label or email or account_id,
        "auth_type": "oauth",
        "provider": PROVIDER,
        "priority": priority,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "email": email,
        "expires_at_ms": 0,
    }
    # Omit account_user_id when deliberately empty so the "missing field"
    # code path is exercised in _account_user_id / duplicate_account_indices.
    if account_user_id is None:
        account_user_id = f"user-{account_id}" if account_id else ""
    if account_user_id:
        credential["account_user_id"] = account_user_id
    return credential


def _stored(store: ConfigStore) -> list[dict[str, Any]]:
    """Read the provider's credential list back through the decryption path."""
    with store._conn as conn:  # noqa: SLF001
        cursor = conn.execute(
            "SELECT credentials FROM tusker_config_oauth_credentials WHERE provider = ?",
            (PROVIDER,),
        )
        row = cursor.fetchone()
    assert row is not None, "no credential row written"
    return json.loads(store._decrypt(conn, row[0]))  # noqa: SLF001


def _stub_flow(monkeypatch: pytest.MonkeyPatch, credential: dict[str, Any]) -> None:
    monkeypatch.setattr(
        module, "_run_device_flow", lambda label, max_polls: credential
    )


# ---------------------------------------------------------------------------
# duplicate_account_indices
# ---------------------------------------------------------------------------


def test_duplicate_indices_detects_matching_login() -> None:
    pool = [_credential(account_id="acct-A"), _credential(account_id="acct-B")]
    incoming = _credential(account_id="acct-B", refresh_token="rt-NEW", email="x@y")
    assert duplicate_account_indices(pool, incoming) == [1]


def test_duplicate_indices_reports_every_entry_sharing_a_login() -> None:
    # The 2026-09-22 incident: three re-enrollments of one ChatGPT login.
    pool = [
        _credential(account_id="acct-A", email="a@b.c"),
        _credential(account_id="acct-A", email="d@e.f", cred_id="cred-2"),
        _credential(account_id="acct-A", email="g@h.i", cred_id="cred-3"),
    ]
    incoming = _credential(account_id="acct-A", email="j@k.l", refresh_token="rt-NEW")
    assert duplicate_account_indices(pool, incoming) == [0, 1, 2]


def test_duplicate_indices_allows_distinct_logins_on_one_team_account() -> None:
    """The 2026-09-22 finding: ChatGPT Team members share an account_id
    but carry distinct account_user_ids and may co-enroll without conflict."""
    pool = [
        _credential(account_id="acct-A", account_user_id="user-damien"),
        _credential(account_id="acct-A", account_user_id="user-bob"),
    ]
    incoming = _credential(account_id="acct-A", account_user_id="user-alice")
    assert duplicate_account_indices(pool, incoming) == []


def _legacy_jwt(
    account_id: str = "acct-A",
    email: str = "legacy@x.y",
    *,
    account_user_id: str | None = None,
) -> str:
    """Minimal unsigned JWT carrying the ChatGPT account claim."""
    import base64

    def b64(segment: dict[str, Any]) -> str:
        raw = json.dumps(segment).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    auth: dict[str, str] = {"chatgpt_account_id": account_id}
    if account_user_id:
        auth["chatgpt_account_user_id"] = account_user_id
    payload = {
        "https://api.openai.com/auth": auth,
        "email": email,
    }
    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def test_duplicate_indices_ignores_legacy_entry_without_user_identity() -> None:
    """Truly ancient entries that carry only a chatgpt_account_id (no
    chatgpt_account_user_id or user_id) are treated as distinct.  The
    circuit breaker handles their dead refresh tokens gracefully."""
    legacy = {
        "id": "legacy-1",
        "label": "legacy",
        "auth_type": "oauth",
        "provider": PROVIDER,
        "access_token": "at-1",
        "refresh_token": "rt-old",
        "id_token": _legacy_jwt(account_id="acct-A"),
        "expires_at_ms": 0,
    }
    incoming = _credential(account_id="acct-A", refresh_token="rt-NEW")
    # No match: the legacy entry has no resolvable account_user_id
    assert duplicate_account_indices([legacy], incoming) == []


def test_duplicate_indices_ignores_entries_without_login_identity() -> None:
    """Entries lacking account_user_id (and no resolvable JWT) are never
    treated as duplicates — they may be legacy or from another provider."""
    pool = [_credential(account_user_id="", access_token="opaque-tok")]
    assert duplicate_account_indices(pool, _credential(account_id="acct-A")) == []
    assert duplicate_account_indices([], _credential(account_id="acct-A")) == []


def test_duplicate_indices_ignores_non_jwt_legacy_tokens() -> None:
    """Copilot-style opaque tokens carry no resolvable ChatGPT identity."""
    pool = [{"token": "gho_x", "access_token": "gho_x"}]
    assert duplicate_account_indices(pool, _credential(account_id="acct-A")) == []


def test_account_user_id_resolves_from_stored_field() -> None:
    cred = _credential(account_user_id="user-XYZ")
    assert module._account_user_id(cred) == "user-XYZ"


def test_account_user_id_resolves_from_jwt_fallback() -> None:
    jwt = _legacy_jwt(account_id="acct-A", account_user_id="user-damien")
    cred = {"access_token": jwt, "id_token": jwt}
    assert module._account_user_id(cred) == "user-damien"


def test_account_user_id_omitted_when_empty() -> None:
    """Credentials with account_id but no resolvable user identity should
    omit the account_user_id field (safe default for legacy tokens)."""
    cred = _credential(account_user_id="")
    assert "account_user_id" not in cred


def test_email_falls_back_to_jwt_profile() -> None:
    from tusker_gateway.tools.enroll_codex_credential import _email

    legacy = {
        "access_token": "at-1",
        "id_token": _legacy_jwt(email="legacy@x.y"),
    }
    assert _email(legacy) == "legacy@x.y"



# ---------------------------------------------------------------------------
# select_slot
# ---------------------------------------------------------------------------


def test_select_slot_explicit_index() -> None:
    pool = [_credential(), _credential(email="b@c.d", cred_id="cred-2")]
    assert select_slot(pool, _credential(email="x@y"), slot=1) == 1


def test_select_slot_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="out of range"):
        select_slot([_credential()], _credential(), slot=5)


def test_select_slot_reuses_email_slot_then_appends() -> None:
    pool = [_credential(email="a@b.c"), _credential(email="x@y", cred_id="cred-2")]
    # Same account+email re-enrollment lands back in its own slot...
    assert select_slot(pool, _credential(email="a@b.c")) == 0
    # ...a brand-new email appends.
    assert select_slot(pool, _credential(email="new@e.f")) == 2


def test_select_slot_honours_replace_email() -> None:
    pool = [_credential(email="a@b.c"), _credential(email="x@y", cred_id="cred-2")]
    assert select_slot(pool, _credential(email="other@z"), replace_email="x@y") == 1


def test_select_slot_matches_refresh_token() -> None:
    pool = [_credential(refresh_token="rt-OLD", email="a@b.c")]
    incoming = _credential(refresh_token="rt-OLD", email="a@b.c")
    assert select_slot(pool, incoming, replace_token="rt-OLD") == 0


# ---------------------------------------------------------------------------
# write_credentials
# ---------------------------------------------------------------------------


def test_write_credentials_round_trips(store: ConfigStore) -> None:
    pool = [_credential(email="a@b.c"), _credential(email="x@y", cred_id="cred-2")]
    write_credentials(store, PROVIDER, pool)
    assert _stored(store) == pool


def test_write_credentials_bumps_generation(store: ConfigStore) -> None:
    with store._conn as conn:  # noqa: SLF001
        before = conn.execute(
            "SELECT generation FROM tusker_config_meta WHERE id = 1"
        ).fetchone()[0]
    write_credentials(store, PROVIDER, [_credential()])
    with store._conn as conn:  # noqa: SLF001
        after = conn.execute(
            "SELECT generation FROM tusker_config_meta WHERE id = 1"
        ).fetchone()[0]
    assert after > before


def test_write_credentials_creates_row_when_absent(store: ConfigStore) -> None:
    write_credentials(store, PROVIDER, [_credential(email="a@b.c")])
    assert [c["email"] for c in _stored(store)] == ["a@b.c"]


def test_write_credentials_replaces_existing_row(store: ConfigStore) -> None:
    write_credentials(store, PROVIDER, [_credential(email="old@e.com")])
    write_credentials(store, PROVIDER, [_credential(email="new@e.com", cred_id="new")])
    assert [c["email"] for c in _stored(store)] == ["new@e.com"]


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_refuses_unmanaged_store(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class _Unmanaged:
        _managed = False

    monkeypatch.setattr(module, "ConfigStore", lambda *a, **k: _Unmanaged())
    assert module.main(argv=[]) == 1


def test_main_dry_run_leaves_store_untouched(
    store: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_credentials(store, PROVIDER, [_credential(email="a@b.c", account_id="acct-A")])
    with store._conn as conn:  # noqa: SLF001
        before = conn.execute(
            "SELECT generation FROM tusker_config_meta WHERE id = 1"
        ).fetchone()[0]

    _stub_flow(monkeypatch, _credential(account_id="acct-B", email="b@c.d"))

    assert module.main(argv=["--dry-run"]) == 0
    with store._conn as conn:  # noqa: SLF001
        after = conn.execute(
            "SELECT generation FROM tusker_config_meta WHERE id = 1"
        ).fetchone()[0]
    assert after == before
    assert [c["email"] for c in _stored(store)] == ["a@b.c"]


def test_main_appends_distinct_account(
    store: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_credentials(store, PROVIDER, [_credential(email="a@b.c", account_id="acct-A")])
    _stub_flow(monkeypatch, _credential(account_id="acct-B", email="b@c.d"))

    assert module.main(argv=["--label", "second"]) == 0
    assert [c["email"] for c in _stored(store)] == ["a@b.c", "b@c.d"]


def test_main_appends_distinct_logins_on_same_team_account(
    store: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-09-22 feature: ChatGPT Team members share an account_id but
    carry distinct account_user_ids, so they must co-enroll by default."""
    write_credentials(
        store,
        PROVIDER,
        [_credential(email="a@b.c", account_id="acct-A", account_user_id="user-damien")],
    )
    _stub_flow(
        monkeypatch,
        _credential(account_id="acct-A", account_user_id="user-teammate", email="b@c.d"),
    )

    assert module.main(argv=[]) == 0
    stored = _stored(store)
    assert len(stored) == 2
    assert [c["email"] for c in stored] == ["a@b.c", "b@c.d"]
    assert [c["account_user_id"] for c in stored] == ["user-damien", "user-teammate"]


def test_main_refuses_second_entry_for_same_login(
    store: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the misconfiguration that killed the whole codex pool.
    One ChatGPT login must never appear twice in the pool."""
    write_credentials(
        store,
        PROVIDER,
        [_credential(email="a@b.c", account_id="acct-A", account_user_id="user-1")],
    )
    # The SAME login re-enrolling under a new label/email.
    _stub_flow(
        monkeypatch,
        _credential(account_id="acct-A", account_user_id="user-1", email="other@e.f"),
    )

    assert module.main(argv=[]) == 1
    assert [c["email"] for c in _stored(store)] == ["a@b.c"]

    # Explicit override still works, for an operator who wants it anyway.
    assert module.main(argv=["--allow-duplicate-account"]) == 0
    assert [c["email"] for c in _stored(store)] == ["a@b.c", "other@e.f"]


def test_main_replaces_same_email_slot_in_place(
    store: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_credentials(
        store,
        PROVIDER,
        [
            _credential(email="a@b.c", account_id="acct-A", cred_id="keep-me", priority=3),
            _credential(email="x@y", account_id="acct-B", cred_id="cred-2"),
        ],
    )
    _stub_flow(
        monkeypatch,
        _credential(
            email="a@b.c", account_id="acct-A", refresh_token="rt-NEW", cred_id="fresh"
        ),
    )

    assert module.main(argv=[]) == 0
    stored = _stored(store)
    assert len(stored) == 2  # replaced in place, not appended
    # Slot identity and routing priority survive a re-enrollment.
    assert stored[0]["id"] == "keep-me"
    assert stored[0]["priority"] == 3
    assert stored[0]["refresh_token"] == "rt-NEW"


def test_main_replaces_indexed_slot(
    store: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_credentials(
        store,
        PROVIDER,
        [
            _credential(email="a@b.c", account_id="acct-A", cred_id="cred-1"),
            _credential(email="x@y", account_id="acct-B", cred_id="cred-2"),
        ],
    )
    _stub_flow(monkeypatch, _credential(account_id="acct-B", email="x@y", refresh_token="rt-NEW"))

    assert module.main(argv=["--slot", "1"]) == 0
    stored = _stored(store)
    assert stored[1]["refresh_token"] == "rt-NEW"
    assert stored[1]["id"] == "cred-2"




def test_collapse_duplicates_removes_sharing_entries() -> None:
    pool = [
        _credential(email="a@b.c", account_id="acct-A", cred_id="keep", priority=2),
        _credential(email="x@y", account_id="acct-B", cred_id="other"),
        _credential(email="d@e.f", account_id="acct-A", cred_id="dup"),
    ]
    incoming = _credential(account_id="acct-A", email="new@e.f", cred_id="fresh")

    updated, removed = module.collapse_duplicates(pool, incoming)

    assert removed == [0, 2]
    assert len(updated) == 2
    assert updated[0]["email"] == "new@e.f"  # took over the first removed slot
    assert updated[0]["id"] == "keep"  # identity carried over
    assert updated[0]["priority"] == 2
    assert updated[1]["email"] == "x@y"  # unrelated entry preserved


def test_collapse_duplicates_appends_when_no_match() -> None:
    pool = [_credential(email="a@b.c", account_id="acct-A")]
    incoming = _credential(account_id="acct-B", email="b@c.d")

    updated, removed = module.collapse_duplicates(pool, incoming)

    assert removed == []
    assert [c["email"] for c in updated] == ["a@b.c", "b@c.d"]


def test_main_collapse_account_fixes_duplicated_pool(
    store: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real remediation: one fresh credential replaces the 3-dead-copies pool."""
    write_credentials(
        store,
        PROVIDER,
        [
            _credential(email="dmascord@g", account_id="acct-A", cred_id="slot0"),
            _credential(email="damien.01@g", account_id="acct-A", cred_id="slot1"),
            _credential(email="damien.02@g", account_id="acct-A", cred_id="slot2"),
        ],
    )
    _stub_flow(
        monkeypatch,
        _credential(account_id="acct-A", email="dmascord@g", refresh_token="rt-FRESH"),
    )

    assert module.main(argv=["--collapse-account", "--label", "dmascord"]) == 0
    stored = _stored(store)
    assert len(stored) == 1  # one entry per account
    assert stored[0]["email"] == "dmascord@g"
    assert stored[0]["refresh_token"] == "rt-FRESH"
    assert stored[0]["id"] == "slot0"  # slot identity preserved
def test_main_ignores_pool_without_account_ids(
    store: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copilot-style token-only entries carry no account_id and never collide."""
    store_pool = [{"token": "gho_x"}]
    write_credentials(store, "github-copilot", store_pool)
    _stub_flow(monkeypatch, _credential(account_id="acct-A", email="a@b.c"))

    assert module.main(argv=[]) == 0
    assert [c["email"] for c in _stored(store)] == ["a@b.c"]
    with store._conn as conn:  # noqa: SLF001
        cursor = conn.execute(
            "SELECT credentials FROM tusker_config_oauth_credentials WHERE provider = ?",
            ("github-copilot",),
        )
        assert json.loads(store._decrypt(conn, cursor.fetchone()[0])) == store_pool  # noqa: SLF001


def test_module_exposes_main() -> None:
    assert callable(sys.modules[module.__name__].main)