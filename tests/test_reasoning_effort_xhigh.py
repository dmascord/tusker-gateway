"""Regression coverage for `reasoning_effort` normalization per provider.

`xhigh` is an OpenAI/Codex-specific reasoning level. Most other upstreams
(mlx-mac, apim, alibaba, ...) reject it with "invalid reasoning value:
'xhigh'", which fails the whole turn. The gateway downgrades `xhigh` to
`high` for those providers and keeps it for the backends that accept it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tusker_gateway.passthrough import (
    _normalize_reasoning_effort,
    PassthroughClient,
)
from tusker_gateway.quality import QualityDB


def _client(**overrides) -> PassthroughClient:
    config = {
        "api_keys": ["gw-test-key"],
        "provider_api_keys": {"mlx-mac": "mlx-key", "opencode-go": "ocg-key"},
        "codex_credentials": [
            {"access_token": "codex-access-token", "account_id": "acct-a"},
        ],
        "quality_db_path": ":memory:",
    }
    config.update(overrides)
    return PassthroughClient(config, QualityDB(":memory:"), MagicMock())


@pytest.mark.parametrize(
    "provider",
    ["mlx-mac", "apim", "alibaba", "synthetic", "zai", "groq", "google"],
)
def test_xhigh_is_downgraded_for_providers_that_reject_it(provider):
    """A provider that cannot parse `xhigh` must receive a supported level."""
    assert _normalize_reasoning_effort("xhigh", provider) == "high"


@pytest.mark.parametrize("provider", ["openai-codex", "opencode-go"])
def test_xhigh_is_preserved_for_providers_that_accept_it(provider):
    """Codex and OpenCode Go accept `xhigh` natively."""
    assert _normalize_reasoning_effort("xhigh", provider) == "xhigh"


def test_xhigh_is_downgraded_when_the_provider_is_unknown():
    """An unknown provider gets the conservative downgrade, not the raw level."""
    assert _normalize_reasoning_effort("xhigh", None) == "high"
    assert _normalize_reasoning_effort("xhigh", "") == "high"


def test_provider_matching_is_case_insensitive():
    """A differently-cased provider name still gets the native level."""
    assert _normalize_reasoning_effort("xhigh", "OpenAI-Codex") == "xhigh"
    assert _normalize_reasoning_effort("xhigh", "MLX-Mac") == "high"


def test_other_reasoning_levels_are_not_downgraded():
    """Only `xhigh` is downgraded; other levels reach the upstream unchanged."""
    for level in ("low", "medium", "high", "max", "adaptive"):
        assert _normalize_reasoning_effort(level, "mlx-mac") == level


def test_minimal_and_maximal_aliases_still_map():
    """The pre-existing client aliases keep their mapping."""
    assert _normalize_reasoning_effort("minimal", "opencode-go") == "low"
    assert _normalize_reasoning_effort("maximal", "opencode-go") == "max"


def test_non_string_effort_is_left_alone():
    """A non-string effort value is not rewritten."""
    assert _normalize_reasoning_effort(None, "mlx-mac") is None
    assert _normalize_reasoning_effort(5, "mlx-mac") == 5


@pytest.mark.asyncio
async def test_chat_request_downgrades_xhigh_for_unsupported_provider():
    """The chat-completions body must carry a level the upstream accepts."""
    client = _client()
    endpoint = {
        "base_url": "https://api.mlx-mac.example/v1",
        "chat_path": "/chat/completions",
        "auth_type": "bearer",
    }

    _headers, body = await client._build_request(
        "mlx-mac",
        "qwen3.8-27b",
        [{"role": "user", "content": "hi"}],
        stream=False,
        api_key="mlx-key",
        extra_headers=None,
        extra_body={"reasoning_effort": "xhigh"},
        endpoint=endpoint,
    )

    assert body["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_chat_request_keeps_xhigh_for_opencode_go():
    """OpenCode Go accepts the native level, so it must survive unchanged."""
    client = _client()
    endpoint = {
        "base_url": "https://opencode.go/api/v1",
        "chat_path": "/chat/completions",
        "auth_type": "bearer",
    }

    _headers, body = await client._build_request(
        "opencode-go",
        "deepseek-v4-pro",
        [{"role": "user", "content": "hi"}],
        stream=False,
        api_key="ocg-key",
        extra_headers=None,
        extra_body={"reasoning_effort": "xhigh"},
        endpoint=endpoint,
    )

    assert body["reasoning_effort"] == "xhigh"


def _codex_response() -> MagicMock:
    response = MagicMock()
    response.status = 200
    response.headers = {}
    response.release = MagicMock()

    async def _content():
        yield (
            b'data: {"type":"response.completed","response":{"output":[{"type":"message",'
            b'"content":[{"type":"output_text","text":"ok"}]}]}}\n\n'
        )

    response.content = _content()
    return response


@pytest.mark.asyncio
async def test_responses_request_downgrades_xhigh_for_unsupported_provider():
    """The Responses path downgrades `xhigh` for a provider that rejects it."""
    client = _client()
    http = MagicMock()
    http.request = AsyncMock(return_value=_codex_response())
    client._http = http
    endpoint = {
        "base_url": "https://api.example/v1",
        "chat_path": "/responses",
        "auth_type": "bearer",
    }

    await client._chat_codex(
        "mlx-mac",
        "qwen3.8-27b",
        [{"role": "user", "content": "hi"}],
        stream=False,
        api_key="mlx-key",
        extra_headers=None,
        extra_body={"reasoning_effort": "xhigh"},
        endpoint=endpoint,
    )

    sent_body = http.request.await_args.kwargs["json"]
    assert sent_body["reasoning"]["effort"] == "high"


@pytest.mark.asyncio
async def test_responses_request_keeps_xhigh_for_codex():
    """Codex keeps the native level it accepts on the Responses path."""
    client = _client(provider_api_keys={})
    http = MagicMock()
    http.request = AsyncMock(return_value=_codex_response())
    client._http = http
    endpoint = {
        "base_url": "https://chatgpt.com/backend-api/codex",
        "chat_path": "/responses",
        "auth_type": "codex",
    }

    await client._chat_codex(
        "openai-codex",
        "gpt-5.6-luna",
        [{"role": "user", "content": "hi"}],
        stream=False,
        api_key=None,
        extra_headers=None,
        extra_body={"reasoning_effort": "xhigh"},
        endpoint=endpoint,
    )

    sent_body = http.request.await_args.kwargs["json"]
    assert sent_body["reasoning"]["effort"] == "xhigh"
