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


def test_unlisted_provider_keeps_provider_wide_behavior():
    tracker = CooldownTracker()

    tracker.cooldown("cohere", "command-r-plus", 60)

    assert tracker.is_cooldown("cohere", "command-r-plus")
    assert tracker.is_cooldown("cohere", "another-model")

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
