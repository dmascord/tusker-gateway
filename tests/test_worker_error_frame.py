"""Regression: worker stream failures must emit real SSE frames.

The worker used ``\\n\\n`` (literal backslash-n) in its error path, so the
gateway received one 160-byte unparseable chunk instead of an error frame plus
[DONE]. See docs/incidents/2026-11-23-claude-code-cli-sse-escape.md.
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tusker_gateway import worker as worker_module


@pytest.mark.asyncio
async def test_worker_stream_failure_emits_parseable_sse_frames(monkeypatch):
    class _FailingAdapter:
        async def chat(self, **_kwargs):
            async def _broken():
                yield b'data: {"partial": true}\n\n'
                raise RuntimeError("upstream exploded")
            return _broken()

    monkeypatch.setattr(
        worker_module.provider_adapters, "get", lambda _name: _FailingAdapter()
    )
    client = TestClient(TestServer(worker_module.create_app()))
    await client.start_server()
    try:
        resp = await client.post(
            "/worker/chat",
            json={
                "provider": "claude-code-cli",
                "model": "sonnet",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert resp.status == 200
        body = await resp.read()
        # Real blank lines, not the two-character sequence backslash-n.
        assert b"\n\n" in body
        assert b"\\n\\n" not in body
        frames = [f for f in body.split(b"\n\n") if f.startswith(b"data:")]
        assert len(frames) == 3  # partial frame, then error, then [DONE]
        assert b'"error"' in frames[-2]
        assert frames[-1] == b"data: [DONE]"
    finally:
        await client.close()
