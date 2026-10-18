from tusker_gateway.cooldown import CooldownTracker


def test_model_scoped_provider_does_not_block_sibling_models():
    tracker = CooldownTracker()

    tracker.cooldown("openrouter", "google/gemma-4-31b-it:free", 60)

    assert tracker.is_cooldown("openrouter", "google/gemma-4-31b-it:free")
    assert not tracker.is_cooldown("openrouter", "openai/gpt-oss-20b:free")


def test_gemini_and_groq_are_model_scoped():
    for provider in ("google", "groq"):
        tracker = CooldownTracker()
        tracker.cooldown(provider, "model-a", 60)

        assert tracker.is_cooldown(provider, "model-a")
        assert not tracker.is_cooldown(provider, "model-b")


def test_provider_scoped_cooldown_still_blocks_provider():
    tracker = CooldownTracker()

    tracker.cooldown("openrouter", "", 300)

    assert tracker.is_cooldown("openrouter", "model-a")
    assert tracker.is_cooldown("openrouter", "model-b")


def test_cohere_model_scoped_cooldown_does_not_block_sibling_models():
    tracker = CooldownTracker()

    tracker.cooldown("cohere", "rerank-v3.5", 60)

    assert tracker.is_cooldown("cohere", "rerank-v3.5")
    assert not tracker.is_cooldown("cohere", "another-model")


def test_model_unavailable_for_integrator_is_detected():
    from tusker_gateway.cooldown import is_model_unavailable_error

    assert is_model_unavailable_error(
        '{"error":{"message":"The requested model is not available for '
        'integrator \\\"copilot-developer-cli\\\"."}}'
    )
    assert not is_model_unavailable_error("model not found")

def test_permanent_failure_marker_covers_404_and_410():
    """404/410 are permanent for this key/account even when the upstream
    catalog still advertises the model (e.g. Google deprecated gemini-2.5-pro
    and gemini-2.0-flash but continues to list them in /v1beta/openai/models).
    """
    from tusker_gateway.cooldown import (
        clear_permanently_failed,
        is_permanently_failed,
        mark_permanently_failed,
    )

    route = ("google", "gemini-2.5-pro")
    try:
        mark_permanently_failed(*route)
        assert is_permanently_failed(*route)
        # Sibling models are unaffected.
        assert not is_permanently_failed("google", "gemini-2.5-flash")
    finally:
        clear_permanently_failed(*route)
        assert not is_permanently_failed(*route)


def _provider_error(message: str = "boom", code: str | None = None):
    from tusker_gateway.errors import ProviderError

    error = ProviderError(message, code=code)
    return error


def test_provider_error_honours_explicit_retry_after_secs():
    """A local transport (CLI adapter) that knows its exact reset window
    (``resets 4am (UTC)``) drives the breaker cooldown directly."""
    from tusker_gateway.cooldown import _cooldown_seconds_for_provider_error

    error = _provider_error("limit hit", code="claude_code_cli_quota")
    error.upstream_body = "You've hit your session limit · resets 4am (UTC)"
    error.retry_after_secs = 9187.0

    assert _cooldown_seconds_for_provider_error(error) == 9187.0


def test_cli_quota_code_gets_quota_cooldown_without_http_status():
    from tusker_gateway.cooldown import _cooldown_seconds_for_provider_error

    error = _provider_error("limit hit", code="claude_code_cli_quota")
    error.upstream_body = "You've hit your session limit"

    assert _cooldown_seconds_for_provider_error(error) == 3600.0


def test_generic_cli_failure_keeps_policy_fallback():
    from tusker_gateway.cooldown import _cooldown_seconds_for_provider_error

    assert _cooldown_seconds_for_provider_error(
        _provider_error("Claude Code CLI request failed", code="claude_code_cli_failed")
    ) is None


def test_5xx_and_permanent_status_semantics_unchanged():
    from tusker_gateway.cooldown import (
        PERMANENT_ERROR_COOLDOWN_SECS,
        _cooldown_seconds_for_provider_error,
    )

    transient = _provider_error("overloaded", code="upstream_error")
    transient.upstream_status = 503
    assert _cooldown_seconds_for_provider_error(transient) is None

    permanent = _provider_error("gone", code="not_found")
    permanent.upstream_status = 404
    assert _cooldown_seconds_for_provider_error(permanent) == PERMANENT_ERROR_COOLDOWN_SECS


def test_repeated_5xx_escalates_past_the_policy_cooldown():
    """A route failing 5xx hundreds of times must not be re-probed on the
    same 60s cadence as one that failed once."""
    from tusker_gateway.cooldown import _cooldown_seconds_for_provider_error

    error = _provider_error("overloaded", code="upstream_error")
    error.upstream_status = 503

    # First few failures stay on the breaker policy cooldown.
    for count in (0, 1, 2, 3):
        assert _cooldown_seconds_for_provider_error(
            error, consecutive_failures=count
        ) is None
    # Then the window grows along the ladder.
    assert _cooldown_seconds_for_provider_error(error, consecutive_failures=4) == 60.0
    assert _cooldown_seconds_for_provider_error(error, consecutive_failures=5) == 300.0
    assert _cooldown_seconds_for_provider_error(error, consecutive_failures=6) == 3600.0
    # And saturates at the longest step rather than growing without bound.
    assert _cooldown_seconds_for_provider_error(error, consecutive_failures=500) == 3600.0


def test_status_less_transport_failure_escalates_too():
    """CLI/transport errors carry no upstream status; repeated failures still
    escalate instead of probing forever on the policy cooldown."""
    from tusker_gateway.cooldown import _cooldown_seconds_for_provider_error

    error = _provider_error("Claude Code CLI request failed", code="claude_code_cli_failed")
    assert _cooldown_seconds_for_provider_error(error) is None
    assert _cooldown_seconds_for_provider_error(error, consecutive_failures=4) == 60.0
    assert _cooldown_seconds_for_provider_error(error, consecutive_failures=6) == 3600.0


def test_quota_hint_on_5xx_still_earns_the_quota_window():
    """A quota exhaustion surfaced on a 5xx is not a transient blip."""
    from tusker_gateway.cooldown import _cooldown_seconds_for_provider_error

    error = _provider_error("upstream error", code="upstream_error")
    error.upstream_status = 503
    error.upstream_body = '{"error":{"message":"monthly limit reached"}}'

    assert _cooldown_seconds_for_provider_error(error, consecutive_failures=99) == 3600.0


def test_transient_escalation_ladder_is_configurable():
    from tusker_gateway.cooldown import (
        _cooldown_seconds_for_provider_error,
        _transient_escalation_ladder,
    )

    error = _provider_error("overloaded", code="upstream_error")
    error.upstream_status = 502
    monkey_env = {"TUSKER_RETRY_TRANSIENT_ESCALATION_SECS": "90,900"}
    original = __import__("os").environ.copy()
    try:
        __import__("os").environ.update(monkey_env)
        assert _transient_escalation_ladder() == (90.0, 900.0)
        assert _cooldown_seconds_for_provider_error(error, consecutive_failures=4) == 90.0
        assert _cooldown_seconds_for_provider_error(error, consecutive_failures=5) == 900.0
        assert _cooldown_seconds_for_provider_error(error, consecutive_failures=9) == 900.0
    finally:
        __import__("os").environ.clear()
        __import__("os").environ.update(original)


def test_malformed_escalation_ladder_falls_back_to_defaults():
    from tusker_gateway.cooldown import (
        DEFAULT_TRANSIENT_ESCALATION_SECS,
        _transient_escalation_ladder,
    )

    original = __import__("os").environ.copy()
    try:
        __import__("os").environ["TUSKER_RETRY_TRANSIENT_ESCALATION_SECS"] = "60,not-a-number"
        assert _transient_escalation_ladder() == DEFAULT_TRANSIENT_ESCALATION_SECS
        __import__("os").environ["TUSKER_RETRY_TRANSIENT_ESCALATION_SECS"] = "0,60"
        assert _transient_escalation_ladder() == DEFAULT_TRANSIENT_ESCALATION_SECS
    finally:
        __import__("os").environ.clear()
        __import__("os").environ.update(original)
