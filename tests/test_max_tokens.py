"""Unit tests for tusker_gateway.max_tokens reasoning floor."""

from __future__ import annotations

from tusker_gateway.max_tokens import (
    DEFAULT_FLOOR,
    REASONING_FLOOR,
    apply_max_tokens_floor,
    is_reasoning_model,
    max_tokens_floor,
)


class TestIsReasoningModel:
    def test_qwen3_is_reasoning(self):
        assert is_reasoning_model("mlx-community/Qwen3-8B-4bit")
        assert is_reasoning_model("qwen3-30b-a3b")

    def test_qwopus_is_reasoning(self):
        assert is_reasoning_model("Jackrong/MLX-Qwopus3.5-9B-v3-4bit")
        assert is_reasoning_model("Jackrong/MLX-Qwopus3.5-27B-v3-4bit")

    def test_openai_o1_o3_o4_are_reasoning(self):
        assert is_reasoning_model("o1-preview")
        assert is_reasoning_model("o1-mini")
        assert is_reasoning_model("o3-mini")
        assert is_reasoning_model("o4-mini")

    def test_deepseek_r1_is_reasoning(self):
        assert is_reasoning_model("deepseek-r1-distill-llama-70b")
        assert is_reasoning_model("deepseek-r1")

    def test_anthropic_claude_opus_is_reasoning(self):
        assert is_reasoning_model("claude-opus-4")
        assert is_reasoning_model("claude-opus-4-5")

    def test_google_gemini_25_is_reasoning(self):
        assert is_reasoning_model("gemini-2.5-pro")
        assert is_reasoning_model("gemini-2.5-flash")

    def test_minimax_is_reasoning(self):
        # All current MiniMax M-series models emit inline <think> blocks
        # and need the floor.
        assert is_reasoning_model("MiniMax-M3")
        assert is_reasoning_model("MiniMax-M2.7")
        assert is_reasoning_model("MiniMax-M2.7-highspeed")
        assert is_reasoning_model("minimax-m3")
        assert is_reasoning_model("MiniMax.M3")

    def test_non_reasoning_models(self):
        assert not is_reasoning_model("gpt-4o")
        assert not is_reasoning_model("gpt-3.5-turbo")
        assert not is_reasoning_model("claude-sonnet-4")
        assert not is_reasoning_model("claude-3-5-sonnet-20241022")
        assert not is_reasoning_model("llama3.2:3b")
        assert not is_reasoning_model("qwen2.5-coder:7b")
        assert not is_reasoning_model("qwen2.5:3b")

    def test_case_insensitive(self):
        assert is_reasoning_model("QWEN3-8B")
        assert is_reasoning_model("Qwen3-8B")

    def test_empty_string(self):
        assert not is_reasoning_model("")


class TestMaxTokensFloor:
    def test_reasoning_returns_floor(self):
        assert max_tokens_floor("qwen3-8b") == REASONING_FLOOR
        assert REASONING_FLOOR == 2000

    def test_non_reasoning_returns_zero(self):
        assert max_tokens_floor("gpt-4o") == 0
        assert max_tokens_floor("llama3.2:3b") == 0
        assert max_tokens_floor("") == DEFAULT_FLOOR
        assert DEFAULT_FLOOR == 0


class TestApplyMaxTokensFloor:
    def test_non_reasoning_unchanged(self):
        extra = {"max_tokens": 20, "temperature": 0}
        out = apply_max_tokens_floor(extra, "openrouter", "gpt-4o")
        assert out is extra

    def test_reasoning_bumps_below_floor(self):
        extra = {"max_tokens": 20, "temperature": 0}
        out = apply_max_tokens_floor(extra, "mlx-mac", "qwen3-8b")
        assert out is not extra
        assert out["max_tokens"] == REASONING_FLOOR
        assert out["temperature"] == 0  # other fields preserved

    def test_reasoning_at_or_above_floor_unchanged(self):
        for value in (REASONING_FLOOR, REASONING_FLOOR + 100, 4096):
            extra = {"max_tokens": value}
            out = apply_max_tokens_floor(extra, "mlx-mac", "qwen3-8b")
            assert out is extra, f"value {value} should be unchanged"

    def test_max_completion_tokens_is_handled_by_build_extra_body(self):
        # apply_max_tokens_floor only looks at max_tokens; the
        # max_completion_tokens → max_tokens rename happens upstream
        # in _build_extra_body.
        extra = {"max_tokens": 100, "max_completion_tokens": 50}
        out = apply_max_tokens_floor(extra, "mlx-mac", "qwen3-8b")
        assert out["max_tokens"] == REASONING_FLOOR
        assert out["max_completion_tokens"] == 50

    def test_missing_max_tokens_for_reasoning(self):
        # No max_tokens at all → no bump (upstream defaults apply).
        # The caller gets a full response from upstream's own default.
        extra = {"temperature": 0}
        out = apply_max_tokens_floor(extra, "mlx-mac", "qwen3-8b")
        assert out is extra

    def test_none_extra_body_returns_empty(self):
        out = apply_max_tokens_floor(None, "mlx-mac", "qwen3-8b")
        assert out == {}

    def test_does_not_mutate_input(self):
        extra = {"max_tokens": 20}
        snapshot = dict(extra)
        apply_max_tokens_floor(extra, "mlx-mac", "qwen3-8b")
        assert extra == snapshot

    def test_provider_name_does_not_matter(self):
        # Reasoning detection is model-slug based; provider is logged only.
        a = apply_max_tokens_floor({"max_tokens": 20}, "mlx-mac", "qwen3-8b")
        b = apply_max_tokens_floor({"max_tokens": 20}, "openai-codex", "qwen3-8b")
        assert a["max_tokens"] == b["max_tokens"] == REASONING_FLOOR
