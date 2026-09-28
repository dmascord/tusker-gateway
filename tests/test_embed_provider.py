"""Jetson-first embedding routing and fallback behavior."""
from __future__ import annotations

import json
from typing import Any

import pytest

from tusker_gateway.cooldown import global_tracker
from tusker_gateway.errors import BadRequestError, ProviderError
from tusker_gateway.providers.embed import EmbedBackend, EmbedHandler


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


class _RecordingBreaker:
    """Breaker double that records what the handler reports to it."""

    def __init__(self) -> None:
        self.failures: list[tuple[str, str]] = []
        self.successes: list[tuple[str, str]] = []

    def record_failure(self, provider: str, model: str) -> None:
        self.failures.append((provider, model))

    def record_success(self, provider: str, model: str) -> None:
        self.successes.append((provider, model))

    def check(self, provider: str, model: str) -> Any:
        class _Decision:
            allowed = True

        return _Decision()


def _openrouter_backend() -> EmbedBackend:
    return EmbedBackend(
        provider="openrouter",
        url="https://openrouter.test/v1/embeddings",
        model="guard-model",
        api_key="openrouter-test-key",
        dimensions=384,
    )


@pytest.mark.asyncio
async def test_unknown_model_is_rejected_without_contacting_backends(monkeypatch):
    """A model no backend can serve must 400, not broadcast to every backend."""
    monkeypatch.setenv("TUSKER_EMBED_PROVIDERS", "local-llm,synthetic")
    handler = EmbedHandler(_config())
    session = _FakeSession([])

    with pytest.raises(BadRequestError) as excinfo:
        await handler.embed({"model": "hermes-code", "input": "text"}, session=session)

    assert excinfo.value.code == "unsupported_model"
    assert "hermes-embed" in excinfo.value.message
    assert session.calls == []


def test_unknown_provider_pin_is_rejected(monkeypatch):
    monkeypatch.setenv("TUSKER_EMBED_PROVIDERS", "local-llm,synthetic")
    handler = EmbedHandler(_config())

    with pytest.raises(BadRequestError) as excinfo:
        handler.backends_for_model("openai-codex/text-embedding-3-small")

    assert excinfo.value.code == "unsupported_model"


def test_bare_configured_model_pins_to_its_provider(monkeypatch):
    """A configured model name routes to its own provider, with no broadcast."""
    monkeypatch.setenv("TUSKER_EMBED_PROVIDERS", "local-llm,openrouter")
    config = _config()
    config["providers"]["openrouter"] = {
        "kind": "bearer",
        "auth_type": "bearer",
        "base_url": "https://openrouter.test/v1",
        "embed_path": "/embeddings",
    }
    config["provider_api_keys"]["openrouter"] = "openrouter-test-key"
    handler = EmbedHandler(config)

    backends, override = handler.backends_for_model(
        "sentence-transformers/all-MiniLM-L6-v2"
    )

    assert override == "sentence-transformers/all-MiniLM-L6-v2"
    assert [(backend.provider, backend.model) for backend in backends] == [
        ("openrouter", "sentence-transformers/all-MiniLM-L6-v2")
    ]


def test_request_level_rejection_does_not_cool_or_trip_breaker(tmp_path):
    """One unacceptable request must not quarantine the route for everyone."""
    config = _config()
    config["quality_db_path"] = str(tmp_path / "quality.db")
    handler = EmbedHandler(config)
    breaker = _RecordingBreaker()
    tracker = global_tracker()
    error = ProviderError(
        "Embedding provider rejected the request", code="provider_error"
    )
    error.upstream_status = 400
    error.upstream_body = '{"error": {"message": "model not found"}}'
    # White-box: the provider-wide sentinel is not reachable through clear().
    tracker._provider_default.pop("openrouter", None)

    try:
        handler._mark_failure(_openrouter_backend(), error, breaker)

        assert breaker.failures == []
        assert tracker.is_cooldown("openrouter", "guard-model") is False
    finally:
        tracker.clear("openrouter", "guard-model")
        tracker.clear_failures("openrouter")
        tracker._provider_default.pop("openrouter", None)


def test_provider_health_failure_still_cools_and_records(tmp_path):
    config = _config()
    config["quality_db_path"] = str(tmp_path / "quality.db")
    handler = EmbedHandler(config)
    breaker = _RecordingBreaker()
    tracker = global_tracker()
    error = ProviderError(
        "Embedding provider authentication failed", code="auth_error"
    )
    error.upstream_status = 401

    try:
        handler._mark_failure(_openrouter_backend(), error, breaker)

        assert breaker.failures == [("openrouter", "guard-model")]
        assert tracker.is_cooldown("openrouter", "guard-model") is True
    finally:
        tracker.clear("openrouter", "guard-model")
        tracker.clear_failures("openrouter")
        tracker._provider_default.pop("openrouter", None)


def test_quota_shaped_rejection_still_cools(tmp_path):
    """A quota body describes the account, so the long window still applies."""
    config = _config()
    config["quality_db_path"] = str(tmp_path / "quality.db")
    handler = EmbedHandler(config)
    breaker = _RecordingBreaker()
    tracker = global_tracker()
    error = ProviderError(
        "Embedding provider rejected the request", code="provider_error"
    )
    error.upstream_status = 404
    error.upstream_body = '{"error": "free-models-per-day quota exceeded"}'

    try:
        handler._mark_failure(_openrouter_backend(), error, breaker)

        assert tracker.is_cooldown("openrouter", "guard-model") is True
    finally:
        tracker.clear("openrouter", "guard-model")
        tracker.clear_failures("openrouter")
        tracker._provider_default.pop("openrouter", None)
