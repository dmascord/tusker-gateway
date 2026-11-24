"""Regression tests for the token-diet investigation changes (2026-11-24).

Covers four fixes:

1. Context-pressure advisory headers (``tusker_gateway/context_advisory.py``)
   emitted on ``/v1/chat/completions`` and ``/v1/messages``.
2. Anthropic ``document`` blocks fail closed instead of shipping the base64
   payload as stringified prose (``translators/anthropic.py``).
3. RTK pytest/cargo filters keep the failure reason, and the documented
   ``X-Rtk-Token-Saver: off`` header actually disables compression.
4. ``Counter.sum_where`` (dashboard token card backing).
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from .conftest import HEADERS_AUTH


# ---------------------------------------------------------------------------
# 1. Context advisory headers
# ---------------------------------------------------------------------------


class TestContextAdvisoryUnit:
    def _body(self, model: str, message: str) -> dict:
        return {
            "model": model,
            "max_tokens": 100,
            "messages": [{"role": "user", "content": message}],
        }

    def test_estimate_always_emitted_for_known_route(self, monkeypatch):
        from tusker_gateway.context_advisory import context_advisory_headers

        # minimax is a configured provider with a known window.
        headers = context_advisory_headers(
            [{"role": "user", "content": "hello"}],
            provider="minimax",
            model="MiniMax-M3",
        )
        assert headers["X-Tusker-Context-Estimate"] > "0"
        assert "X-Tusker-Context-Window" in headers
        assert "X-Tusker-Context-Remaining" in headers
        assert "X-Tusker-Context-Used-Pct" in headers
        assert "X-Tusker-Context-Warning" not in headers

    def test_warning_fires_above_threshold(self, monkeypatch):
        from tusker_gateway.context_advisory import context_advisory_headers

        # Pin a small window so the threshold is reached deliberately rather
        # than depending on a provider's published context length.
        monkeypatch.setenv(
            "TUSKER_CONTEXT_WINDOW_OVERRIDES_JSON",
            '{"testprov/testmodel": 100000}',
        )
        monkeypatch.setenv("TUSKER_CONTEXT_ADVISORY_PCT", "80")
        big = "x" * (3 * 120_000)  # ~112k tokens by the 3.2 chars/token rule
        headers = context_advisory_headers(
            [{"role": "user", "content": big}],
            provider="testprov",
            model="testmodel",
        )
        assert headers["X-Tusker-Context-Window"] == "100000"
        assert "X-Tusker-Context-Warning" in headers
        warning = headers["X-Tusker-Context-Warning"]
        assert "context window" in warning
        assert int(headers["X-Tusker-Context-Used-Pct"]) >= 80

    def test_no_warning_below_threshold(self):
        from tusker_gateway.context_advisory import context_advisory_headers

        headers = context_advisory_headers(
            [{"role": "user", "content": "hello"}],
            provider="minimax",
            model="MiniMax-M3",
        )
        assert "X-Tusker-Context-Warning" not in headers

    def test_disabled_by_env(self, monkeypatch):
        from tusker_gateway.context_advisory import context_advisory_headers

        monkeypatch.setenv("TUSKER_CONTEXT_ADVISORY", "0")
        headers = context_advisory_headers(
            [{"role": "user", "content": "hello"}],
            provider="minimax",
            model="MiniMax-M3",
        )
        assert headers == {}

    def test_estimate_only_when_window_unknown(self):
        from tusker_gateway.context_advisory import context_advisory_headers

        headers = context_advisory_headers(
            [{"role": "user", "content": "hello"}],
            provider="provider-that-does-not-exist",
            model="no-such-model",
        )
        estimate = int(headers.get("X-Tusker-Context-Estimate", "0"))
        assert estimate >= 1
        assert "X-Tusker-Context-Window" not in headers
        assert "X-Tusker-Context-Warning" not in headers

    def test_never_raises_on_garbage(self):
        from tusker_gateway.context_advisory import context_advisory_headers

        headers = context_advisory_headers([{"role": "user", "content": {"weird": object()}}])
        assert isinstance(headers, dict)


@pytest.mark.asyncio
async def test_chat_completions_emits_advisory_headers(client):
    openai_response = {
        "id": "chatcmpl-adv",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    with patch(
        "tusker_gateway.passthrough.PassthroughClient.chat", new_callable=AsyncMock
    ) as mock_chat:
        mock_chat.return_value = openai_response
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "minimax::MiniMax-M3",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=HEADERS_AUTH,
        )
    assert resp.status == 200
    assert resp.headers.get("X-Tusker-Context-Estimate") is not None
    assert resp.headers.get("X-Tusker-Context-Window") is not None


@pytest.mark.asyncio
async def test_messages_emits_advisory_headers(client):
    openai_response = {
        "id": "chatcmpl-adv-a",
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    with patch(
        "tusker_gateway.anthropic_adapter.PassthroughClient.chat", new_callable=AsyncMock
    ) as mock_chat:
        mock_chat.return_value = openai_response
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-3-opus-20240229",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers=HEADERS_AUTH,
        )
    assert resp.status == 200
    assert resp.headers.get("X-Tusker-Context-Estimate") is not None


# ---------------------------------------------------------------------------
# 2. Anthropic document blocks fail closed
# ---------------------------------------------------------------------------


def test_document_block_rejected_not_stringified():
    from tusker_gateway.translators.anthropic import request_anthropic_to_openai
    from tusker_gateway.errors import BadRequestError

    body = {
        "model": "claude-sonnet-4",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Summarize"},
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": "JVBERi0xLjQK",
                        },
                    },
                ],
            }
        ],
    }
    with pytest.raises(BadRequestError) as excinfo:
        request_anthropic_to_openai(body)
    assert excinfo.value.code == "unsupported_document_block"


def test_document_block_oversized_payload_never_forwarded():
    """A 1.5 MB base64 PDF must not enter the upstream message body."""
    from tusker_gateway.translators.anthropic import request_anthropic_to_openai
    from tusker_gateway.errors import BadRequestError

    body = {
        "model": "claude-sonnet-4",
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": "JVBERi0xLjQK" * 120_000,
                        },
                    }
                ],
            }
        ],
    }
    with pytest.raises(BadRequestError):
        request_anthropic_to_openai(body)


@pytest.mark.asyncio
async def test_messages_document_block_rejected_at_http_layer(client):
    resp = await client.post(
        "/v1/messages",
        json={
            "model": "claude-3-opus-20240229",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": "JVBERi0xLjQK",
                            },
                        }
                    ],
                }
            ],
        },
        headers=HEADERS_AUTH,
    )
    assert resp.status == 400
    data = await resp.json()
    assert data["error"]["type"] == "invalid_request_error"


# ---------------------------------------------------------------------------
# 3. RTK hardening
# ---------------------------------------------------------------------------


PYTEST_WITH_REASON = "\n".join(
    [
        "============================= test session starts ==============================",
        "collected 12 items",
        "",
        "tests/test_bar.py::test_baz FAILED",
        "",
        "=================================== FAILURES ===================================",
        "_______________________________ test_baz _______________________________",
        "",
        "    def test_baz():",
        ">       assert compute(3) == 4",
        "E       AssertionError: assert 3 == 4",
        "",
        "=========================== short test summary info ============================",
        "FAILED tests/test_bar.py::test_baz - AssertionError: assert 3 == 4",
        "=== 3 failed, 12 passed in 1.42s ===",
    ]
) + "\n"


class TestRtkPytestKeepsReason:
    def test_keeps_assertion_source(self):
        from tusker_gateway.rtk import compress_text

        out = compress_text(PYTEST_WITH_REASON)
        assert ">       assert compute(3) == 4" in out

    def test_keeps_assertion_explanation(self):
        from tusker_gateway.rtk import compress_text

        out = compress_text(PYTEST_WITH_REASON)
        assert "E       AssertionError: assert 3 == 4" in out

    def test_keeps_summary_reason_suffix(self):
        from tusker_gateway.rtk import compress_text

        out = compress_text(PYTEST_WITH_REASON)
        assert "FAILED tests/test_bar.py::test_baz - AssertionError: assert 3 == 4" in out

    def test_still_compresses(self):
        from tusker_gateway.rtk import compress_text

        out = compress_text(PYTEST_WITH_REASON)
        assert len(out) < len(PYTEST_WITH_REASON)
        assert "collected 12 items" not in out


class TestRtkLargeBlocks:
    def test_large_git_diff_still_compressed(self):
        """Blocks above the old 64 KiB cap must still be compressed."""
        from tusker_gateway.rtk import compress_text

        lines = ["diff --git a/x.py b/x.py"]
        for i in range(4000):
            lines.extend([
                "--- a/x.py",
                "+++ b/x.py",
                f"@@ -{i},1 +{i},1 @@",
                f"-old line {i}",
                f"+new line {i}",
            ])
        big = "\n".join(lines) + "\n"
        assert len(big) > 64 * 1024
        out = compress_text(big)
        assert len(out) < len(big)


@pytest.mark.asyncio
async def test_rtk_token_saver_header_opts_out(client):
    """The documented X-Rtk-Token-Saver: off header must disable compression."""
    rtk = pytest.importorskip("tusker_gateway.rtk")
    rtk.set_enabled(True)
    try:
        openai_response = {
            "id": "chatcmpl-rtk",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        big_tool_output = "\n".join(
            f"total {i}\ndrwxr-xr-x  1 user staff  {i} Aug 25 12:00 dir{i}"
            for i in range(200)
        )
        messages = [
            {"role": "user", "content": "list files"},
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": big_tool_output},
        ]
        with patch(
            "tusker_gateway.passthrough.PassthroughClient.chat", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = openai_response
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "minimax::MiniMax-M3",
                    "max_tokens": 100,
                    "messages": messages,
                },
                headers={**HEADERS_AUTH, "X-Rtk-Token-Saver": "off"},
            )
        assert resp.status == 200
        sent = mock_chat.call_args.kwargs.get("messages") or mock_chat.call_args[0][2]
        assert sent[2]["content"] == big_tool_output
    finally:
        rtk.set_enabled(False)


# ---------------------------------------------------------------------------
# 4. Counter.sum_where
# ---------------------------------------------------------------------------


def test_counter_sum_where_filters_labels():
    from tusker_gateway.metrics import Counter

    c = Counter("t", "test", ("pool", "direction"))
    c.inc({"pool": "a", "direction": "prompt"}, 100)
    c.inc({"pool": "b", "direction": "prompt"}, 50)
    c.inc({"pool": "a", "direction": "completion"}, 10)
    assert c.sum_where(direction="prompt") == 150.0
    assert c.sum_where(direction="completion") == 10.0
    assert c.sum_where() == 160.0
    assert c.sum_where(pool="a") == 110.0


def test_dashboard_quota_renders_token_card():
    """The dashboard token card must render without a metrics registry or
    with one, and must not crash when empty."""
    import asyncio

    from tusker_gateway.dashboard import dashboard_quota
    from tusker_gateway.metrics import MetricsRegistry

    async def _run():
        from aiohttp.test_utils import make_mocked_request

        # Without metrics registered.
        request = make_mocked_request("GET", "/dashboard/partials/quota", app={})
        resp = await dashboard_quota(request)
        text = resp.text or ""
        assert isinstance(text, str)

        registry = MetricsRegistry()
        registry.tokens_total.inc(
            {"pool": "code", "provider": "p", "model": "m", "direction": "prompt"}, 1234
        )
        registry.tokens_total.inc(
            {"pool": "code", "provider": "p", "model": "m", "direction": "completion"}, 22
        )
        request2 = make_mocked_request(
            "GET", "/dashboard/partials/quota", app={"metrics": registry}
        )
        resp2 = await dashboard_quota(request2)
        text2 = resp2.text or ""
        assert "tokens" in text2
        assert "1,234" in text2
        assert "22" in text2

    asyncio.run(_run())
