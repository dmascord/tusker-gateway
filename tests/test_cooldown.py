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
