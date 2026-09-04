"""Tests for the provider-aware /v1/rerank pathway."""
from __future__ import annotations

import json
from typing import Any

import pytest

from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
from tusker_gateway.providers.rerank import RerankHandler


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
