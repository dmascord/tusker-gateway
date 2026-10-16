"""Tests for the Phase 0 client-profile normalization resolver.

The resolver is the only thing the test suite can observe in Phase 0
(shadow-only; the dispatcher in Phase 1 is not yet wired). We assert:

* The locked 8-rule precedence (header > body > first-message > UA)
* The ambiguous-UA skip for curl / python-requests / httpx
* Fingerprint width derivation (32 / 16 / 12 hex chars)
* Propagation through AuthMiddleware.verify (request['client_profile'] + ContextVar)
* Idempotent ``record_resolved`` semantics (one count per request)
* The ``shadow_mode_enabled`` env-var contract
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from tusker_gateway.normalization import (
    ClientProfile,
    ResolvedProfile,
    current,
    fingerprint_api_key_64,
    fingerprint_short,
    record_resolved,
    record_shadow_mismatch,
    resolve_profile,
    reset_counters_for_tests,
    set_current,
    shadow_mismatch_snapshot,
    shadow_mode_enabled,
)
from tusker_gateway import normalization as _norm


# --- precedence ----------------------------------------------------------


@pytest.mark.parametrize(
    "name, headers, body, expected_profile, expected_source",
    [
        # Rule 1: X-Tusker-Harness header
        (
            "X-Tusker-Harness header wins",
            {"X-Tusker-Harness": "kilo/7", "User-Agent": "opencode-ai/1.0"},
            None,
            "kilo",
            "header",
        ),
        # Rule 2: X-Client-Harness (kilo) header
        (
            "X-Client-Harness header wins over UA",
            {"X-Client-Harness": "kilo/7", "User-Agent": "opencode-ai/1.0"},
            None,
            "kilo",
            "header",
        ),
        # Rule 2 precedence: X-Tusker-Harness beats X-Client-Harness
        (
            "X-Tusker-Harness > X-Client-Harness",
            {"X-Tusker-Harness": "omp/1", "X-Client-Harness": "kilo/7"},
            None,
            "omp",
            "header",
        ),
        # Rule 3: body.harness
        (
            "body.harness beats UA",
            {"User-Agent": "opencode-ai/1.0"},
            {"harness": "claude-code/2"},
            "claude-code",
            "body",
        ),
        # Rule 3: body.metadata.harness
        (
            "body.metadata.harness beats UA",
            {"User-Agent": "opencode-ai/1.0"},
            {"metadata": {"harness": "kilo/7"}},
            "kilo",
            "body",
        ),
        # Rule 3 precedence: body.harness > body.metadata.harness
        (
            "body.harness > body.metadata.harness",
            {},
            {"harness": "opencode/2", "metadata": {"harness": "kilo/7"}},
            "opencode",
            "body",
        ),
        # Rule 4: empty header value falls through
        (
            "empty X-Tusker-Harness falls through",
            {"X-Tusker-Harness": "", "User-Agent": "opencode-ai/1.0"},
            None,
            "opencode",
            "ua",
        ),
        # Rule 4: unknown header value falls through
        (
            "unknown X-Tusker-Harness value falls through",
            {"X-Tusker-Harness": "random-tool/9", "User-Agent": "opencode-ai/1.0"},
            None,
            "opencode",
            "ua",
        ),
        # Rule 5: clear OpenAI UA
        (
            "OpenAI Python UA is default-openai",
            {"User-Agent": "OpenAI/Python 1.51.0"},
            {"messages": [{"role": "user", "content": "hi"}]},
            "default-openai",
            "ua",
        ),
        # Rule 5: kilo UA
        (
            "kilo UA is kilo",
            {"User-Agent": "kilo-cli/7.8"},
            None,
            "kilo",
            "ua",
        ),
        # Rule 5: codex_cli UA
        (
            "codex_cli UA is codex_cli",
            {"User-Agent": "codex_cli/0.0.0"},
            None,
            "codex_cli",
            "ua",
        ),
        # Rule 5: omp-tunnel UA beats curl ambiguity
        (
            "omp-tunnel UA beats curl/ substring",
            {"User-Agent": "curl/8.0 (omp-tunnel)"},
            None,
            "omp",
            "ua",
        ),
        # Rule 6: ambiguous UA + chat-style first message -> default-openai via tie-breaker
        (
            "curl + ?  -> default-openai via first_message",
            {"User-Agent": "curl/8.0"},
            {"messages": [{"role": "user", "content": "What is the weather?"}]},
            "default-openai",
            "first_message",
        ),
        (
            "python-requests + ?  -> default-openai via first_message",
            {"User-Agent": "python-requests/2.31"},
            {"messages": [{"role": "user", "content": "Summarize this for me."}]},
            "default-openai",
            "first_message",
        ),
        # Rule 6 absent: ambiguous UA + non-question body -> default
        (
            "curl + JSON body -> default",
            {"User-Agent": "curl/8.0"},
            {"messages": [{"role": "user", "content": '{"q":1}'}]},
            "default",
            "default",
        ),
        (
            "python-requests + JSON body -> default",
            {"User-Agent": "python-requests/2.31"},
            {"messages": [{"role": "user", "content": '{"q":1}'}]},
            "default",
            "default",
        ),
        # Rule 7: empty / generic UA -> default
        (
            "Mozilla browser UA -> default",
            {"User-Agent": "Mozilla/5.0"},
            None,
            "default",
            "default",
        ),
        # Rule 7: missing body & no clear UA -> default
        (
            "no body, no UA -> default",
            {},
            None,
            "default",
            "default",
        ),
        # Rule 2 beats Rule 3 (header > body)
        (
            "X-Client-Harness beats body.harness",
            {"X-Client-Harness": "kilo/7"},
            {"harness": "opencode/2"},
            "kilo",
            "header",
        ),
    ],
)
def test_resolver_precedence(name, headers, body, expected_profile, expected_source):
    result = resolve_profile(headers=headers, body=body, api_key="sk-test")
    assert result.profile.value == expected_profile, name
    assert result.detection_source == expected_source, name


# --- fingerprint derivation ---------------------------------------------


def test_fingerprint_short_widths():
    fp = "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
    assert fingerprint_short(fp, width=32) == fp[:32]
    assert fingerprint_short(fp, width=16) == fp[:16]
    assert fingerprint_short(fp, width=12) == fp[:12]


def test_fingerprint_short_rejects_unsupported_width():
    fp = "a" * 64
    with pytest.raises(ValueError, match="unsupported fingerprint width"):
        fingerprint_short(fp, width=10)


def test_fingerprint_api_key_64_is_sha256_hex():
    fp = fingerprint_api_key_64("sk-test")
    assert len(fp) == 64
    int(fp, 16)  # round-trip parseable
    # Same input -> same fingerprint (deterministic).
    assert fingerprint_api_key_64("sk-test") == fp


def test_fingerprint_api_key_64_distinct_inputs_distinct_fps():
    a = fingerprint_api_key_64("sk-a")
    b = fingerprint_api_key_64("sk-b")
    assert a != b


def test_resolved_profile_carries_fingerprints():
    result = resolve_profile(
        headers={"User-Agent": "opencode-ai/1.0"},
        body=None,
        api_key="sk-test",
    )
    assert result.key_fingerprint_64 == fingerprint_api_key_64("sk-test")
    assert result.key_fingerprint_short == fingerprint_api_key_64("sk-test")[:16]


def test_resolved_profile_no_api_key_yields_empty_fingerprints():
    result = resolve_profile(
        headers={"User-Agent": "opencode-ai/1.0"},
        body=None,
        api_key=None,
    )
    # Empty string is the "no key" sentinel; access log treats it as a missing
    # field via the same ``or "unknown"`` fallback used elsewhere.
    assert result.key_fingerprint_64 == ""
    assert result.key_fingerprint_short == ""

# --- ambiguous-UA guard --------------------------------------------------


def test_user_agent_substring_skips_default_openai_for_ambiguous_uas():
    """curl/8.0 must NOT collapse to default-openai via UA; first-message tie-breaker runs."""
    from tusker_gateway.normalization import _user_agent_substring

    for ua in ("curl/8.0", "curl/7.81.0", "python-requests/2.31", "httpx/0.27"):
        assert _user_agent_substring(ua) is None, ua


def test_user_agent_substring_still_matches_clear_openai_consumer():
    from tusker_gateway.normalization import _user_agent_substring

    assert _user_agent_substring("OpenAI/Python 1.51.0") == ClientProfile.DEFAULT_OPENAI
    assert _user_agent_substring("openai-node/4.0.0") == ClientProfile.DEFAULT_OPENAI


# --- counter contract ----------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_normalization_counters():
    reset_counters_for_tests()
    yield
    reset_counters_for_tests()


def test_record_resolved_increments_per_profile():
    r1 = resolve_profile(headers={"User-Agent": "kilo-cli/7.8"}, body=None, api_key="x")
    record_resolved(r1)
    record_resolved(r1)

    r2 = resolve_profile(headers={"User-Agent": "opencode-ai/1.0"}, body=None, api_key="x")
    record_resolved(r2)

    snap = _norm.profile_counts_snapshot()
    assert snap["kilo"] == 2
    assert snap["opencode"] == 1


def test_record_resolved_callers_responsible_for_idempotency():
    """``record_resolved`` does NOT self-dedupe; the auth-middleware / handler
    pair uses ``_client_profile_recorded`` to ensure the same request only
    counts once. This test pins that contract: two bare calls count twice."""
    r = resolve_profile(headers={"User-Agent": "kilo-cli/7.8"}, body=None, api_key="x")
    record_resolved(r)
    record_resolved(r)
    assert _norm.profile_counts_snapshot()["kilo"] == 2


def test_record_shadow_mismatch_keyed_by_profile_and_surface():
    record_shadow_mismatch(ClientProfile.KILO, "chat_request")
    record_shadow_mismatch(ClientProfile.KILO, "chat_request")
    record_shadow_mismatch(ClientProfile.OPENCODE, "error_envelope")
    snap = shadow_mismatch_snapshot()
    assert snap[("kilo", "chat_request")] == 2
    assert snap[("opencode", "error_envelope")] == 1


# --- ContextVar ----------------------------------------------------------


def test_context_var_round_trip():
    r = resolve_profile(headers={"User-Agent": "kilo-cli/7.8"}, body=None, api_key="x")
    set_current(r)
    cur = current()
    assert isinstance(cur, ResolvedProfile)
    assert cur.profile == ClientProfile.KILO


# --- shadow-mode env-var -------------------------------------------------


def test_shadow_mode_default_on(monkeypatch):
    monkeypatch.delenv("TUSKER_NORMALIZATION_SHADOW", raising=False)
    assert shadow_mode_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "  0 "])
def test_shadow_mode_disables_on_falsy(value, monkeypatch):
    monkeypatch.setenv("TUSKER_NORMALIZATION_SHADOW", value)
    assert shadow_mode_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_shadow_mode_stays_on_for_truthy(value, monkeypatch):
    monkeypatch.setenv("TUSKER_NORMALIZATION_SHADOW", value)
    assert shadow_mode_enabled() is True


# --- AuthMiddleware propagation -----------------------------------------


def test_auth_middleware_attaches_client_profile():
    """``AuthMiddleware.verify`` must populate ``request['client_profile']``
    and the ContextVar before returning. Verified end-to-end via a fake
    request with a matching API key, so we don't depend on app wiring."""
    from tusker_gateway.auth import AuthMiddleware

    # Load a config with one known API key.
    config = {
        "api_keys": ["sk-prod-test-key"],
        "identity_stores": {},
        "audit": {"enabled": False, "fail_closed": False, "hmac_key": ""},
        "config_db_enabled": False,
        "config_db_url": None,
    }
    middleware = AuthMiddleware(identity_store=None)

    fake_request = _FakeRequest(
        headers={"User-Agent": "kilo-cli/7.8", "Authorization": "Bearer sk-prod-test-key"},
        app_config=config,
    )

    asyncio.run(middleware.verify(fake_request))

    assert "client_profile" in fake_request
    profile = fake_request["client_profile"]
    assert profile.profile == ClientProfile.KILO
    assert profile.detection_source == "ua"
    assert profile.key_fingerprint_64 == fingerprint_api_key_64("sk-prod-test-key")

    # ContextVar must reflect the same profile.
    assert current().profile == ClientProfile.KILO


def test_auth_middleware_keeps_default_openai_for_ambiguous_ua_plus_question():
    """Even with header-only resolution at verify time, an ambiguous UA
    (curl) collapses to ``default`` until the body is read. The first
    request pass logs ``default``; the handler upgrades to
    ``default-openai`` after reading the body. Both writes are valid;
    the access log captures the latest one."""
    from tusker_gateway.auth import AuthMiddleware

    config = {
        "api_keys": ["sk-prod-test-key"],
        "identity_stores": {},
        "audit": {"enabled": False, "fail_closed": False, "hmac_key": ""},
        "config_db_enabled": False,
        "config_db_url": None,
    }
    middleware = AuthMiddleware(identity_store=None)
    fake_request = _FakeRequest(
        headers={"User-Agent": "curl/8.0", "Authorization": "Bearer sk-prod-test-key"},
        app_config=config,
    )
    asyncio.run(middleware.verify(fake_request))
    # At verify time, body is unknown -> curl is ambiguous -> default.
    assert fake_request["client_profile"].profile == ClientProfile.DEFAULT
    assert fake_request["client_profile"].detection_source == "default"


class _FakeRequest:
    """Minimal aiohttp request shim for unit-testing middleware in isolation."""

    def __init__(self, headers: dict, app_config: dict) -> None:
        self.headers = _CIMultiDict(headers)
        self._app = {"config": app_config}
        self._dict: dict = {}

    def __setitem__(self, key, value):
        self._dict[key] = value

    def __getitem__(self, key):
        return self._dict[key]

    def get(self, key, default=None):
        return self._dict.get(key, default)

    @property
    def app(self) -> dict:
        return self._app

    def __contains__(self, key):
        # Middleware uses both ``request["x"]`` and ``"x" in request``; both
        # routes must succeed against the same backing dict.
        return key in self._dict



class _CIMultiDict:
    """Subset of aiohttp CIMultiDict used by the auth middleware."""

    def __init__(self, headers: dict) -> None:
        self._headers = headers

    def get(self, name, default=None):
        for k, v in self._headers.items():
            if k.lower() == name.lower():
                return v
        return default

    def __iter__(self):
        return iter(self._headers)

    def __contains__(self, name):
        return any(k.lower() == name.lower() for k in self._headers)
