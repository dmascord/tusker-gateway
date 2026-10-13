"""A body that is not valid JSON is a client error on every JSON endpoint.

When a handler parses the body inside its generic failure path, the caller is
told the upstream provider failed (502) and the access log records the wrong
``failure_class`` for a request that never reached a provider.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway.endpoints import embeddings_handler, images_handler

from .conftest import HEADERS_AUTH, _create_test_app

MALFORMED_BODY = b'{"model": "hermes-code",'


@pytest_asyncio.fixture
async def media_client():
    """Client for the media routes the shared test app does not register."""
    app = _create_test_app()
    app.router.add_post("/v1/images/generations", images_handler)
    app.router.add_post("/v1/embeddings", embeddings_handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/rerank",
    ],
)
@pytest.mark.asyncio
async def test_malformed_json_body_is_client_error(client, path):
    response = await client.post(
        path,
        data=MALFORMED_BODY,
        headers={**HEADERS_AUTH, "Content-Type": "application/json"},
    )
    body = await response.read()

    assert response.status == 400
    payload = json.loads(body)
    assert payload["error"]["code"] == "malformed_payload"
    assert payload["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_malformed_chat_body_never_reaches_a_provider(client):
    with patch(
        "tusker_gateway.endpoints.PassthroughClient.chat", new_callable=AsyncMock
    ) as mock_chat:
        response = await client.post(
            "/v1/chat/completions",
            data=MALFORMED_BODY,
            headers={**HEADERS_AUTH, "Content-Type": "application/json"},
        )
        await response.read()

    assert response.status == 400
    assert mock_chat.await_count == 0


@pytest.mark.asyncio
async def test_non_utf8_body_is_client_error(client):
    response = await client.post(
        "/v1/chat/completions",
        data=b"\xff\xfe\x00bad",
        headers={**HEADERS_AUTH, "Content-Type": "application/json"},
    )

    assert response.status == 400
    payload = json.loads(await response.read())
    assert payload["error"]["code"] == "malformed_payload"


@pytest.mark.asyncio
async def test_anthropic_malformed_json_body_is_client_error(client):
    """The Anthropic route reports errors in the Messages API shape."""
    response = await client.post(
        "/v1/messages",
        data=MALFORMED_BODY,
        headers={**HEADERS_AUTH, "Content-Type": "application/json"},
    )
    payload = await response.json()

    assert response.status == 400
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_media_and_embedding_malformed_json_body_is_client_error(media_client):
    for path in ("/v1/images/generations", "/v1/embeddings"):
        response = await media_client.post(
            path,
            data=MALFORMED_BODY,
            headers={**HEADERS_AUTH, "Content-Type": "application/json"},
        )
        payload = json.loads(await response.read())

        assert response.status == 400, path
        assert payload["error"]["code"] == "malformed_payload", path