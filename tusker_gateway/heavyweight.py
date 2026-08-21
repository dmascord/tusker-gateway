"""Heavyweight-model classification.

Mirrors hermes-agent's approach (api_server.py:3008-3030): a hardcoded
slug override set combined with an optional pricing-based heuristic.
A model is heavyweight if it's either in the slug set OR above the
pricing thresholds (when models.dev data is available).

Heavyweight is used as a per-pool gate:
  - code, privacy pools drop heavyweights (cheap-tier rotation)
  - premium, swarm pools keep them

This lets operators point gpt-5.6-luna (a non-heavyweight codex slug)
at the privacy pool without having to manually re-curate the pool
list whenever OpenAI ships a new heavyweight Codex slug.
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


# Subset of `_HERMES_HEAVY_MODEL_OVERRIDES` from hermes-agent
# (api_server.py:3008-3013). Mirrored here so that pool gating stays
# in sync with what hermes-agent considers expensive/subscription-tier.
#
# Note: gpt-5.6-luna is intentionally NOT in this set — it's the
# cheap-tier codex slug hermes-agent exposes for privacy/premium.
HEAVYWEIGHT_SLUG_OVERRIDES: frozenset[str] = frozenset({
    # OpenAI Codex (chatgpt.com/backend-api/codex)
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.5",
    "gpt-5.4",
    # Anthropic Claude (paid tiers)
    "claude-sonnet-4.6",
    "claude-opus-4.6",
    "claude-sonnet-4.5",
    "claude-opus-4.5",
    # Google Gemini (paid tiers; the cheap flash variants are NOT here)
    "gemini-2.5-pro",
    "gemini-3-pro",
    # Cohere Command-A family
    "command-a-plus-05-2026",
    "command-a-03-2025",
    # Misc big models
    "mistral-large-3:675b",
    "deepseek-v4-pro",
})


# Pricing thresholds for the dynamic classifier (per 1M tokens).
# A model is heavyweight if EITHER input >= HEAVY_INPUT_USD or output
# >= HEAVY_OUTPUT_USD. Mirrors hermes-agent's models_dev.py thresholds.
HEAVY_INPUT_USD: float = 1.0
HEAVY_OUTPUT_USD: float = 8.0


def is_heavyweight_slug(slug: str) -> bool:
    """Return True if the slug is in the heavyweight override set.

    Slug matching is exact (case-sensitive) — we don't fuzzy-match because
    the slug set is curated by humans.
    """
    return slug in HEAVYWEIGHT_SLUG_OVERRIDES


def is_heavyweight_pricing(
    *,
    cost_input: float | None,
    cost_output: float | None,
    input_threshold: float = HEAVY_INPUT_USD,
    output_threshold: float = HEAVY_OUTPUT_USD,
) -> bool:
    """Return True if the pricing is above the heavyweight thresholds.

    Either field missing → treat as non-heavyweight (we can't say).
    Used by the models.dev catalog classifier when we have pricing data.
    """
    if cost_input is None or cost_output is None:
        return False
    return cost_input >= input_threshold or cost_output >= output_threshold


def is_heavyweight(
    slug: str,
    *,
    cost_input: float | None = None,
    cost_output: float | None = None,
) -> bool:
    """Combined heavyweight check.

    Checks the slug override first (cheap, exact match); falls through to
    the pricing heuristic if pricing data is provided.
    """
    if is_heavyweight_slug(slug):
        return True
    return is_heavyweight_pricing(cost_input=cost_input, cost_output=cost_output)


def filter_heavyweight(
    entries: Iterable[dict],
    *,
    keep_heavyweight: bool,
    cost_lookup=None,
) -> list[dict]:
    """Filter a list of pool entries by heavyweight status.

    Args:
        entries: iterable of dicts each with at least {"provider", "model"}.
        keep_heavyweight: if True, keep heavyweight entries; if False, drop them.
        cost_lookup: optional callable(model_id) -> (cost_input, cost_output) | None.
            Used when models.dev pricing data is available.

    Returns:
        Filtered list (preserves order).
    """
    out: list[dict] = []
    for entry in entries:
        model = entry.get("model", "")
        if not model:
            continue
        # Prefer the per-entry heavyweight flag from the config; fall back
        # to slug/pricing classifiers.
        hw: bool | None = entry.get("heavyweight")
        if hw is None:
            if cost_lookup is not None:
                pricing = cost_lookup(model)
                ci, co = (pricing if pricing is not None else (None, None))
            else:
                ci = co = None
            hw = is_heavyweight(model, cost_input=ci, cost_output=co)
        if hw == keep_heavyweight:
            out.append(entry)
    return out
