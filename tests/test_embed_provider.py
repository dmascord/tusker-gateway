"""Jetson-first embedding routing and fallback behavior."""
from __future__ import annotations

import json
from typing import Any

import pytest

from tusker_gateway.providers.embed import EmbedHandler


class _FakeResponse:
    def __init__(self, payload: Any, *, status: int = 200) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = json.dumps(payload)

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def text(self) -> str:
        return self._body


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def _config() -> dict[str, Any]:
    return {
        "providers": {
            "local-llm": {
                "kind": "local",
                "auth_type": "local",
                "base_url": "http://jetson.test:11434",
                "embed_path": "/v1/embeddings",
            },
            "synthetic": {
                "kind": "bearer",
                "base_url": "https://synthetic.test/v1",
                "embed_path": "/embeddings",
            },
        },
        "provider_api_keys": {"synthetic": "synthetic-test-key"},
    }


def _embedding_response(model: str) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"index": 0, "embedding": [0.1, 0.2]}],
        "model": model,
        "usage": {"prompt_tokens": 2, "total_tokens": 2},
    }


@pytest.mark.asyncio
async def test_priority_strategy_keeps_jetson_first(monkeypatch):
    monkeypatch.setenv("TUSKER_EMBED_PROVIDERS", "local-llm,synthetic")
    monkeypatch.setenv("TUSKER_EMBED_STRATEGY", "priority")
    session = _FakeSession(
        [_FakeResponse(_embedding_response("nomic-embed-text")) for _ in range(2)]
    )
    handler = EmbedHandler(_config())

    for _ in range(2):
        provider, model, result = await handler.embed(
            {"model": "hermes-embed", "input": "same route every time"},
            session=session,
        )
        assert (provider, model) == ("local-llm", "nomic-embed-text")
        assert result["data"][0]["embedding"] == [0.1, 0.2]

    assert [call["url"] for call in session.calls] == [
        "http://jetson.test:11434/v1/embeddings",
        "http://jetson.test:11434/v1/embeddings",
    ]


@pytest.mark.asyncio
async def test_priority_strategy_falls_back_if_jetson_is_unavailable(monkeypatch):
    monkeypatch.setenv("TUSKER_EMBED_PROVIDERS", "local-llm,synthetic")
    monkeypatch.setenv("TUSKER_EMBED_STRATEGY", "priority")
    session = _FakeSession(
        [
            _FakeResponse({"error": "Jetson unavailable"}, status=503),
            _FakeResponse(_embedding_response("remote-embed-model")),
        ]
    )
    handler = EmbedHandler(_config())
    # Keep this routing test independent of persistent/global cooldown state.
    handler._mark_failure = lambda *args: None  # type: ignore[method-assign]

    provider, model, _ = await handler.embed(
        {"model": "hermes-embed", "input": "fallback test"},
        session=session,
    )

    assert (provider, model) == ("synthetic", "hf:nomic-ai/nomic-embed-text-v1.5")
    assert [call["url"] for call in session.calls] == [
        "http://jetson.test:11434/v1/embeddings",
        "https://synthetic.test/v1/embeddings",
    ]


def test_bare_nomic_model_pins_to_local_ollama(monkeypatch):
    monkeypatch.setenv("TUSKER_EMBED_PROVIDERS", "local-llm,synthetic")
    handler = EmbedHandler(_config())

    backends, override = handler.backends_for_model("nomic-embed-text")

    assert override == "nomic-embed-text"
    assert [(backend.provider, backend.model) for backend in backends] == [
        ("local-llm", "nomic-embed-text")
    ]


@pytest.mark.asyncio
async def test_round_robin_remains_the_default(monkeypatch):
    monkeypatch.setenv("TUSKER_EMBED_PROVIDERS", "local-llm,synthetic")
    monkeypatch.delenv("TUSKER_EMBED_STRATEGY", raising=False)
    session = _FakeSession(
        [
            _FakeResponse(_embedding_response("nomic-embed-text")),
            _FakeResponse(_embedding_response("remote-embed-model")),
        ]
    )
    handler = EmbedHandler(_config())

    first, _, _ = await handler.embed(
        {"model": "hermes-embed", "input": "round robin one"}, session=session
    )
    second, _, _ = await handler.embed(
        {"model": "hermes-embed", "input": "round robin two"}, session=session
    )

    assert (first, second) == ("local-llm", "synthetic")
