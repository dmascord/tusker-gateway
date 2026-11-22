"""Context-window aware routing and max_tokens fitting (req_7b0d460047e30296)."""

from __future__ import annotations

import json

import pytest

from tusker_gateway import context_limits
from tusker_gateway.context_limits import (
    declared_context_window,
    estimate_prompt_tokens,
    fit_output_budget,
    min_output_tokens,
    record_learned_context_window,
    required_context_tokens,
    set_published_context_windows,
)
from tusker_gateway.errors import ContextWindowExceededError, openai_error


@pytest.fixture(autouse=True)
def _clear_context_window_state(monkeypatch):
    set_published_context_windows({})
    context_limits._learned_context_windows.clear()
    monkeypatch.delenv("TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON", raising=False)
    monkeypatch.delenv("TUSKER_MIN_OUTPUT_TOKENS", raising=False)
    yield
    set_published_context_windows({})
    context_limits._learned_context_windows.clear()
from tusker_gateway.errors import UnusableToolResponseError


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setenv(
        "TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON",
        json.dumps({"mlx-mac": 32768, "mlx-mac/ornith-1.5:35b": 65536}),
    )


def test_model_key_wins_over_provider_key(windows):
    assert declared_context_window("mlx-mac", "ornith-1.5:35b") == 65536
    assert declared_context_window("MLX-Mac", "qwen3.8-27b") == 32768
    # No explicit override: falls back to conservative built-in defaults so
    # large prompts are not forwarded past a route's real upstream window.
    assert declared_context_window("openai-codex", "gpt-5.6-luna") == 128_000
    assert declared_context_window("openai-codex", "some-unknown-model") == 128_000


def test_apim_luna_context_window_is_the_total_window_not_the_output_cap(monkeypatch):
    """APIM gpt-5.6-luna: 1,050,000 total context, 128,000 max OUTPUT.

    The upstream /models row carries no context field, so without the
    model-scoped override the built-in apim 128,000 fallback filtered a
    212K-token privacy request out entirely (req_4ae4a2134158c498, 2026-11-22).
    The 128,000 figure is the output cap, not the prompt window.
    """
    monkeypatch.setenv(
        "TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON",
        json.dumps({"apim/gpt-5.6-luna": 1050000}),
    )
    assert declared_context_window("apim", "gpt-5.6-luna") == 1_050_000
    # Other APIM models keep the conservative built-in; the override is model-scoped.
    assert declared_context_window("apim", "gpt-6-luna") == 128_000

    body, budget = fit_output_budget({"max_tokens": 16384}, "apim", "gpt-5.6-luna", 208379)
    assert body["max_tokens"] == 16384
    assert budget.window == 1_050_000
    assert not budget.clamped


def test_published_and_learned_windows_take_precedence_over_override(monkeypatch):
    monkeypatch.setenv(
        "TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON",
        json.dumps({"openai-codex/gpt-5.6-luna": 64000, "openai-codex": 32000}),
    )
    # Override is backup when nothing is known.
    assert declared_context_window("openai-codex", "gpt-5.6-luna") == 64000

    # Published/catalog window wins over override.
    set_published_context_windows({("openai-codex", "gpt-5.6-luna"): 200000})
    assert declared_context_window("openai-codex", "gpt-5.6-luna") == 200000

    # Learned (observed) window also takes precedence.
    record_learned_context_window("openai-codex", "gpt-5.6-luna", 272000, source="header")
    assert declared_context_window("openai-codex", "gpt-5.6-luna") == 272000

    # Without known info, override remains the backup.
    set_published_context_windows({})
    context_limits._learned_context_windows.clear()
    assert declared_context_window("openai-codex", "other-model") == 32000


def test_latest_tag_is_optional_on_either_side(monkeypatch):
    monkeypatch.setenv(
        "TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON",
        json.dumps({"local-llm": 16384, "local-llm/qwen3:4b": 32768, "local-llm/qwopus-9b-coder-mtp:latest": 32768}),
    )
    assert declared_context_window("local-llm", "qwen3:4b") == 32768
    assert declared_context_window("local-llm", "qwopus-9b-coder-mtp") == 32768
    assert declared_context_window("local-llm", "qwen2.5-coder:7b") == 16384


def test_invalid_overrides_are_ignored(monkeypatch):
    monkeypatch.setenv("TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON", "{not json")
    assert declared_context_window("mlx-mac", "x") is None


def test_estimate_counts_tools_images_and_tool_calls():
    text_only = estimate_prompt_tokens([{"role": "user", "content": "x" * 3200}])
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"d": "y" * 3200}}}]
    with_tools = estimate_prompt_tokens([{"role": "user", "content": "x" * 3200}], tools)
    with_image = estimate_prompt_tokens([
        {"role": "user", "content": [
            {"type": "text", "text": "x" * 3200},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100000}},
        ]},
    ])
    with_call = estimate_prompt_tokens([
        {"role": "user", "content": "x" * 3200},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "t", "arguments": "z" * 3200}},
        ]},
    ])

    assert 1000 <= text_only <= 1100
    assert with_tools >= text_only + 1000
    # Base64 image payloads are counted as a fixed token cost, not by length.
    assert text_only + 1500 <= with_image <= text_only + 1700
    assert with_call >= text_only + 1000


def test_max_tokens_clamped_to_remaining_window(windows):
    body, budget = fit_output_budget({"max_tokens": 32000}, "mlx-mac", "qwen3.8-27b", 20000)

    assert body["max_tokens"] == 32768 - 20000 - 256
    assert budget.clamped and budget.window == 32768


def test_max_completion_tokens_is_clamped_too(windows):
    body, _ = fit_output_budget(
        {"max_completion_tokens": 50000}, "mlx-mac", "qwen3.8-27b", 1000
    )
    assert body["max_completion_tokens"] == 32768 - 1000 - 256


def test_fitting_or_undeclared_routes_are_untouched(windows):
    original = {"max_tokens": 4000}
    body, budget = fit_output_budget(original, "mlx-mac", "qwen3.8-27b", 1000)
    assert body is original and not budget.clamped

    # Unknown provider/model with no built-in default remains untouched.
    body, budget = fit_output_budget({"max_tokens": 900000}, "acme", "mystery-model", 1000)
    assert body["max_tokens"] == 900000 and budget.window is None


def test_builtin_defaults_clamp_oversized_cloud_prompts(windows):
    # Regression: 166k-token prompts were forwarded undeclared/clamped=False,
    # letting upstreams silently truncate earlier context.
    body, budget = fit_output_budget(
        {"max_tokens": 16384}, "github-copilot", "gpt-5.6-luna", 166646
    )
    assert budget.window == 128_000
    assert budget.clamped
    assert body["max_tokens"] == max(1, 128_000 - 166646 - 256)

    # A published window (preferred over override/builtin) prevents premature
    # restriction when the model is known to support more.
    set_published_context_windows({("github-copilot", "gpt-5.6-luna"): 272000})
    body, budget = fit_output_budget(
        {"max_tokens": 16384}, "github-copilot", "gpt-5.6-luna", 166646
    )
    assert budget.window == 272000
    assert not budget.clamped
    assert body["max_tokens"] == 16384

    body, budget = fit_output_budget(
        {"max_tokens": 16384}, "opencode-go", "glm-5.3-flash", 9451
    )
    assert budget.window == 202_752
    assert not budget.clamped
    assert body["max_tokens"] == 16384


def test_unset_max_tokens_is_not_invented(windows):
    body, budget = fit_output_budget({}, "mlx-mac", "qwen3.8-27b", 30000)
    assert "max_tokens" not in body and budget.effective_max_tokens is None


def test_pool_skips_candidate_whose_declared_window_cannot_fit(monkeypatch, tmp_path):
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager

    local = ("openrouter", "openai/gpt-oss-20b:free")
    big = ("openai-codex", "gpt-4.1-luna")
    monkeypatch.setenv(
        "TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON", json.dumps({"openrouter": 32768})
    )
    manager = PoolManager({
        "pools": {
            "code": PoolConfig(name="code", models=[
                {"provider": local[0], "model": local[1]},
                {"provider": big[0], "model": big[1]},
            ]),
        },
        "excluded_providers": [],
        "provider_api_keys": {"openrouter": "k-openrouter"},
        "quality_db_path": str(tmp_path / "q.db"),
    })

    def candidates(context_tokens: int) -> set[tuple[str, str]]:
        picks: set[tuple[str, str]] = set()
        while choice := manager.select("code", context_tokens=context_tokens, excluded=set(picks)):
            picks.add(choice)
        return picks

    # The pool default says 128k, but the route declares 32k.
    assert local not in candidates(required_context_tokens(30000))
    assert big in candidates(required_context_tokens(30000))
    assert local in candidates(required_context_tokens(2000))

    # When context size is the only reason an otherwise eligible route was
    # filtered, expose the largest available window for the client retry path.
    context_failure: dict[str, int] = {}
    assert manager.select(
        "code",
        context_tokens=required_context_tokens(30000),
        allowed_models=(local[1],),
        context_failure=context_failure,
    ) is None
    assert context_failure == {
        "required_context_tokens": required_context_tokens(30000),
        "max_context_window": 32768,
    }


def test_builtin_defaults_filter_candidates_that_cannot_fit(monkeypatch, tmp_path):
    from tusker_gateway.config import PoolConfig
    from tusker_gateway.pools import PoolManager

    small = ("github-copilot", "gpt-5.6-luna")
    big = ("openai-codex", "gpt-4.1-luna")
    monkeypatch.delenv("TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON", raising=False)
    manager = PoolManager({
        "pools": {
            "code": PoolConfig(name="code", models=[
                {"provider": small[0], "model": small[1]},
                {"provider": big[0], "model": big[1]},
            ]),
        },
        "excluded_providers": [],
        "provider_api_keys": {"openai-codex": "k-codex"},
        "quality_db_path": str(tmp_path / "q.db"),
    })
    # 166k prompt cannot fit a 128k builtin window but fits gpt-4.1 (1M).
    choice = manager.select("code", context_tokens=required_context_tokens(166646))
    assert choice == big


def test_min_output_reserve_is_configurable(monkeypatch):
    monkeypatch.setenv("TUSKER_MIN_OUTPUT_TOKENS", "1000")
    assert min_output_tokens() == 1000


def test_context_overflow_error_uses_omp_recovery_trigger():
    exc = ContextWindowExceededError(
        pool="privacy",
        required_context_tokens=210_000,
        max_context_window=128_000,
    )

    payload = openai_error(exc.message, code=exc.code, error_type=exc.error_type)
    assert exc.status == 400
    assert payload["error"]["code"] == "context_length_exceeded"
    assert "context_length_exceeded" in payload["error"]["message"]
    assert "128000 tokens" in payload["error"]["message"]


def test_output_budget_exhaustion_is_not_quarantined(monkeypatch):
    from tusker_gateway import endpoints

    calls = []
    monkeypatch.setattr(
        "tusker_gateway.cooldown.global_tracker",
        lambda: type("T", (), {"cooldown": lambda self, *a: calls.append(a)})(),
    )
    monkeypatch.setattr(endpoints, "_persist_cooldown", lambda *a: calls.append(a))

    exhausted = UnusableToolResponseError(reason="output_budget_exhausted")
    endpoints._quarantine_tool_response_failure({}, "mlx-mac", "ornith-1.5:35b", exhausted)
    assert calls == []

    empty = UnusableToolResponseError(reason="reasoning_only_or_empty")
    endpoints._quarantine_tool_response_failure({}, "mlx-mac", "ornith-1.5:35b", empty)
    assert calls


def test_output_budget_message_does_not_claim_quarantine():
    from tusker_gateway.endpoints import _validation_stream_message

    message = _validation_stream_message(
        "req_x",
        error=UnusableToolResponseError(reason="output_budget_exhausted"),
        provider="mlx-mac",
        model="ornith-1.5:35b",
    )
    assert "context window" in message and "quarantined" not in message


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [("length", "output_budget_exhausted"), ("stop", "reasoning_only_or_empty")],
)
async def test_reasoning_only_stream_reason_reflects_finish_reason(finish_reason, expected):
    from tusker_gateway.endpoints import _normalize_stream

    async def stream():
        for payload in (
            {"choices": [{"delta": {"role": "assistant", "reasoning": "Let me look at the image"}}]},
            {"choices": [{"delta": {}, "finish_reason": finish_reason}]},
        ):
            yield f"data: {json.dumps(payload)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    with pytest.raises(UnusableToolResponseError) as info:
        async for _ in _normalize_stream(
            stream(), provider="mlx-mac", model="ornith-1.5:35b", tools_requested=True
        ):
            pass
    assert info.value.reason == expected
