"""Tests for the heavyweight slug-override module + per-pool gate integration.

Mirrors the heavyweight semantics from hermes-agent
(api_server.py:3008-3030, 3016-3030, 3220):
- Slug override set: explicit list of heavy model slugs
- Per-pool gate: code/privacy drop heavyweights, premium/swarm keep them
"""
from __future__ import annotations

from tusker_gateway.heavyweight import (
    HEAVYWEIGHT_SLUG_OVERRIDES,
    filter_heavyweight,
    is_heavyweight,
    is_heavyweight_pricing,
    is_heavyweight_slug,
)


# ---------------------------------------------------------------------------
# is_heavyweight_slug
# ---------------------------------------------------------------------------


def test_gpt_5_6_sol_is_heavyweight_slug():
    """sol/terra are heavy codex slugs per hermes-agent's mirror set."""
    assert is_heavyweight_slug("gpt-5.6-sol") is True
    assert is_heavyweight_slug("gpt-5.6-terra") is True


def test_gpt_5_6_luna_is_not_heavyweight_slug():
    """luna is the cheap-tier codex slug — must NOT be heavyweight."""
    assert is_heavyweight_slug("gpt-5.6-luna") is False
    assert is_heavyweight_slug("gpt-5.4-mini") is False


def test_claude_opus_is_heavyweight_slug():
    assert is_heavyweight_slug("claude-opus-4.6") is True
    assert is_heavyweight_slug("claude-sonnet-4.6") is True


def test_free_openrouter_models_are_not_heavyweight():
    """Free OpenRouter slugs (`:free` suffix) are not in the override set."""
    assert is_heavyweight_slug("openai/gpt-oss-20b:free") is False
    assert is_heavyweight_slug("google/gemma-4-31b-it:free") is False


# ---------------------------------------------------------------------------
# is_heavyweight_pricing
# ---------------------------------------------------------------------------


def test_pricing_above_threshold_is_heavyweight():
    assert is_heavyweight_pricing(cost_input=2.0, cost_output=10.0) is True
    assert is_heavyweight_pricing(cost_input=1.0, cost_output=8.0) is True  # exactly at threshold


def test_pricing_below_threshold_is_not_heavyweight():
    assert is_heavyweight_pricing(cost_input=0.5, cost_output=2.0) is False
    assert is_heavyweight_pricing(cost_input=0.0, cost_output=0.0) is False


def test_pricing_with_missing_fields_is_not_heavyweight():
    """When pricing data is unknown, default to non-heavyweight so we
    don't accidentally block cheap models with bad metadata."""
    assert is_heavyweight_pricing(cost_input=None, cost_output=2.0) is False
    assert is_heavyweight_pricing(cost_input=2.0, cost_output=None) is False
    assert is_heavyweight_pricing(cost_input=None, cost_output=None) is False


# ---------------------------------------------------------------------------
# is_heavyweight (combined)
# ---------------------------------------------------------------------------


def test_combined_uses_slug_first():
    """Slug override wins even if pricing data is missing."""
    assert is_heavyweight("gpt-5.6-sol") is True
    assert is_heavyweight("gpt-5.6-sol", cost_input=0.1, cost_output=0.1) is True


def test_combined_falls_through_to_pricing():
    """Unknown slugs use pricing data."""
    assert is_heavyweight("some-new-model", cost_input=2.0, cost_output=10.0) is True
    assert is_heavyweight("some-new-model", cost_input=0.5, cost_output=2.0) is False


def test_combined_unknown_slug_no_pricing_is_not_heavyweight():
    """Unknown slug + no pricing → not heavyweight (conservative default)."""
    assert is_heavyweight("totally-unknown-model") is False


# ---------------------------------------------------------------------------
# filter_heavyweight
# ---------------------------------------------------------------------------


def test_filter_drops_heavy_when_keep_false():
    entries = [
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "openai-codex", "model": "gpt-5.4-mini"},
    ]
    out = filter_heavyweight(entries, keep_heavyweight=False)
    models = [e["model"] for e in out]
    assert "gpt-5.6-sol" not in models
    assert "gpt-5.6-luna" in models
    assert "gpt-5.4-mini" in models


def test_filter_keeps_heavy_when_keep_true():
    """``keep_heavyweight=True`` keeps heavy entries and drops light ones.

    This is the inverse of the cheap-tier pool filter — useful for
    premium-only filtering scenarios where you want ONLY heavy models.
    """
    entries = [
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},  # heavy
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},  # light
    ]
    out = filter_heavyweight(entries, keep_heavyweight=True)
    models = [e["model"] for e in out]
    assert "gpt-5.6-sol" in models
    assert "gpt-5.6-luna" not in models  # light entries are dropped


def test_filter_per_entry_override_wins():
    """Per-entry `heavyweight: true` overrides slug-based classifier."""
    entries = [
        # Force a normally-light model to be heavy
        {"provider": "openai-codex", "model": "gpt-5.6-luna", "heavyweight": True},
    ]
    out = filter_heavyweight(entries, keep_heavyweight=False)
    assert out == []  # dropped because of explicit flag
    out = filter_heavyweight(entries, keep_heavyweight=True)
    assert len(out) == 1  # kept


def test_filter_with_pricing_lookup():
    """When a cost_lookup is provided, unknown slugs use pricing."""
    entries = [
        {"provider": "newco", "model": "expensive"},
        {"provider": "newco", "model": "cheap"},
    ]
    lookup = lambda m: (5.0, 20.0) if m == "expensive" else (0.1, 0.5)
    out = filter_heavyweight(entries, keep_heavyweight=False, cost_lookup=lookup)
    models = [e["model"] for e in out]
    assert models == ["cheap"]


def test_filter_preserves_order():
    entries = [
        {"provider": "x", "model": "a"},
        {"provider": "x", "model": "gpt-5.6-sol"},  # heavy
        {"provider": "x", "model": "c"},
    ]
    out = filter_heavyweight(entries, keep_heavyweight=False)
    models = [e["model"] for e in out]
    assert models == ["a", "c"]


def test_filter_skips_entries_without_model():
    entries = [
        {"provider": "x"},  # no model
        {"provider": "x", "model": "gpt-5.6-luna"},
    ]
    out = filter_heavyweight(entries, keep_heavyweight=False)
    models = [e["model"] for e in out]
    assert models == ["gpt-5.6-luna"]


# ---------------------------------------------------------------------------
# Mirror check: make sure the override set stays in sync with hermes-agent
# ---------------------------------------------------------------------------


def test_override_set_includes_expected_slugs():
    """Smoke check that all slugs hermes-agent considers heavy are mirrored."""
    expected = {
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5", "gpt-5.4",
        "claude-sonnet-4.6", "claude-opus-4.6",
    }
    assert expected.issubset(HEAVYWEIGHT_SLUG_OVERRIDES)
