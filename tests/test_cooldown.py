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
