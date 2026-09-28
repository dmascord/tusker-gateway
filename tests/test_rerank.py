"""Tests for the provider-aware /v1/rerank pathway."""
from __future__ import annotations

import json
from typing import Any

import pytest

from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
from tusker_gateway.cooldown import global_tracker
from tusker_gateway.errors import BadRequestError, ProviderError
from tusker_gateway.providers.rerank import RerankBackend, RerankHandler


class _FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
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


def _config(tmp_path, providers: tuple[str, ...]) -> dict[str, Any]:
    return {
        "providers": {
            provider: DEFAULT_PROVIDER_REGISTRY[provider]
            for provider in providers
        },
        "provider_api_keys": {provider: f"{provider}-test-key" for provider in providers},
        "quality_db_path": str(tmp_path / "quality.db"),
    }


@pytest.mark.asyncio
async def test_cohere_v2_request_and_response_are_normalized(tmp_path, monkeypatch):
    monkeypatch.delenv("TUSKER_RERANKER_PROVIDERS", raising=False)
    session = _FakeSession(
        [
            _FakeResponse(
                {
                    "id": "rerank-1",
                    "results": [
                        {"index": 1, "relevance_score": 0.91},
                        {"index": 0, "relevance_score": 0.22},
                    ],
                }
            )
        ]
    )
    handler = RerankHandler(_config(tmp_path, ("cohere",)))

    provider, model, result = await handler.rerank(
        {
            "model": "hermes-reranker",
            "query": "which document answers the question?",
            "documents": ["first document", "second document"],
            "top_n": 2,
        },
        session=session,
    )

    assert (provider, model) == ("cohere", "rerank-v3.5")
    assert result["model"] == "rerank-v3.5"
    assert result["results"] == [
        {"index": 1, "relevance_score": 0.91},
        {"index": 0, "relevance_score": 0.22},
    ]
    call = session.calls[0]
    assert call["url"] == "https://api.cohere.com/v2/rerank"
    assert call["headers"]["Authorization"] == "Bearer cohere-test-key"
    assert call["json"] == {
        "model": "rerank-v3.5",
        "query": "which document answers the question?",
        "documents": ["first document", "second document"],
        "top_n": 2,
    }
    assert "return_documents" not in call["json"]


@pytest.mark.asyncio
async def test_voyage_top_k_and_data_shape_are_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_RERANKER_PROVIDERS", "voyage")
    session = _FakeSession(
        [_FakeResponse({"data": [{"index": 0, "score": 0.77}]})]
    )
    handler = RerankHandler(_config(tmp_path, ("voyage",)))

    provider, model, result = await handler.rerank(
        {
            "model": "voyage::rerank-2.5",
            "query": "query",
            "documents": ["document"],
            "top_k": 1,
            "return_documents": True,
            "truncation": False,
        },
        session=session,
    )

    assert (provider, model) == ("voyage", "rerank-2.5")
    assert result["results"] == [
        {"index": 0, "relevance_score": 0.77, "document": "document"}
    ]
    call = session.calls[0]
    assert call["url"] == "https://api.voyageai.com/v1/rerank"
    assert call["json"]["top_k"] == 1
    assert call["json"]["return_documents"] is True
    assert call["json"]["truncation"] is False
    assert "top_n" not in call["json"]


@pytest.mark.asyncio
async def test_provider_fallback_cools_failed_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_RERANKER_PROVIDERS", "cohere,voyage")
    session = _FakeSession(
        [
            _FakeResponse(
                {"message": "slow down"},
                status=429,
                headers={"Retry-After": "1"},
            ),
            _FakeResponse(
                {"results": [{"index": 0, "relevance_score": 0.5}]}
            ),
        ]
    )
    handler = RerankHandler(_config(tmp_path, ("cohere", "voyage")))

    provider, model, result = await handler.rerank(
        {"query": "query", "documents": ["document"]},
        session=session,
    )

    assert provider == "voyage"
    assert model == "rerank-2"
    assert result["results"][0]["relevance_score"] == 0.5
    assert [call["url"] for call in session.calls] == [
        "https://api.cohere.com/v2/rerank",
        "https://api.voyageai.com/v1/rerank",
    ]


@pytest.mark.asyncio
async def test_malformed_success_response_participates_in_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_RERANKER_PROVIDERS", "cohere,voyage")
    session = _FakeSession(
        [
            _FakeResponse({"ok": True}),
            _FakeResponse(
                {"results": [{"index": 0, "relevance_score": 0.4}]}
            ),
        ]
    )
    handler = RerankHandler(_config(tmp_path, ("cohere", "voyage")))

    provider, _, result = await handler.rerank(
        {"query": "query", "documents": ["document"]},
        session=session,
    )

    assert provider == "voyage"
    assert result["results"][0]["relevance_score"] == 0.4


def test_validation_supports_legacy_document_objects_and_budget():
    request = RerankHandler.validate_request(
        {
            "query": "find title",
            "documents": [
                {"text": "body", "title": "first"},
                {"text": "other", "title": "second"},
            ],
            "rank_fields": ["title", "text"],
            "top_n": 1,
        }
    )

    assert request.documents == ("first\nbody", "second\nother")
    assert request.source_documents[0]["title"] == "first"
    assert request.budget_units > 0


@pytest.mark.asyncio
async def test_rerank_route_requires_auth(client):
    response = await client.post(
        "/v1/rerank",
        json={"query": "query", "documents": ["document"]},
    )
    assert response.status == 401


@pytest.mark.asyncio
async def test_rerank_route_dispatches_and_returns_provider_result(client):
    class _StubReranker:
        async def rerank(self, body, **kwargs):
            return "cohere", "rerank-v3.5", {
                "model": "rerank-v3.5",
                "results": [{"index": 0, "relevance_score": 1.0}],
            }

    client.server.app["rerank_handler"] = _StubReranker()
    response = await client.post(
        "/v1/rerank",
        json={"query": "query", "documents": ["document"]},
        headers={"Authorization": "Bearer sk-secret-dev"},
    )
    assert response.status == 200
    assert (await response.json())["results"][0]["index"] == 0


@pytest.mark.asyncio
async def test_models_advertise_reranker(client):
    response = await client.get(
        "/v1/models",
        headers={"Authorization": "Bearer sk-secret-dev"},
    )
    assert response.status == 200
    ids = {item["id"] for item in (await response.json())["data"]}
    assert "hermes-reranker" in ids


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


def _cohere_backend() -> RerankBackend:
    return RerankBackend(
        provider="cohere",
        url="https://api.cohere.com/v2/rerank",
        model="rerank-v3.5",
        api_key="cohere-test-key",
        style="cohere",
    )


@pytest.mark.asyncio
async def test_unknown_rerank_model_is_rejected_without_contacting_backends(
    tmp_path, monkeypatch
):
    """An unknown model must 400 instead of being broadcast to every backend."""
    monkeypatch.setenv("TUSKER_RERANKER_PROVIDERS", "cohere")
    handler = RerankHandler(_config(tmp_path, ("cohere",)))
    session = _FakeSession([])

    with pytest.raises(BadRequestError) as excinfo:
        await handler.rerank(
            {"model": "x", "query": "query", "documents": ["document"]},
            session=session,
        )

    assert excinfo.value.code == "unsupported_model"
    assert "hermes-reranker" in excinfo.value.message
    assert session.calls == []


def test_bare_configured_rerank_model_pins_to_its_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_RERANKER_PROVIDERS", "cohere")
    monkeypatch.setenv("TUSKER_RERANKER_COHERE_MODEL", "cohere-v4-rerank")
    handler = RerankHandler(_config(tmp_path, ("cohere",)))

    backends, override = handler.backends_for_model("cohere-v4-rerank")

    assert override == "cohere-v4-rerank"
    assert [(backend.provider, backend.model) for backend in backends] == [
        ("cohere", "cohere-v4-rerank")
    ]


def test_request_level_rejection_does_not_cool_or_trip_breaker(tmp_path):
    """One unacceptable request must not quarantine the route for everyone."""
    handler = RerankHandler(_config(tmp_path, ("cohere",)))
    breaker = _RecordingBreaker()
    tracker = global_tracker()
    error = ProviderError(
        "Reranker provider rejected the request", code="provider_error"
    )
    error.upstream_status = 400
    error.upstream_body = '{"message": "documents too large"}'
    # White-box: the provider-wide sentinel is not reachable through clear().
    tracker._provider_default.pop("cohere", None)

    try:
        handler._mark_failure(_cohere_backend(), error, breaker)

        assert breaker.failures == []
        assert tracker.is_cooldown("cohere", "rerank-v3.5") is False
    finally:
        tracker.clear("cohere", "rerank-v3.5")
        tracker.clear_failures("cohere")
        tracker._provider_default.pop("cohere", None)


def test_provider_health_failure_still_cools_and_records(tmp_path):
    handler = RerankHandler(_config(tmp_path, ("cohere",)))
    breaker = _RecordingBreaker()
    tracker = global_tracker()
    error = ProviderError(
        "Reranker provider authentication failed", code="auth_error"
    )
    error.upstream_status = 401

    try:
        handler._mark_failure(_cohere_backend(), error, breaker)

        assert breaker.failures == [("cohere", "rerank-v3.5")]
        assert tracker.is_cooldown("cohere", "rerank-v3.5") is True
    finally:
        tracker.clear("cohere", "rerank-v3.5")
        tracker.clear_failures("cohere")
        tracker._provider_default.pop("cohere", None)
