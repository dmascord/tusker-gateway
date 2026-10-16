"""Tests for the replay_capture CLI tool."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

from tusker_gateway.tools.replay_capture import main
from tusker_gateway.turn_capture import (
    TurnCaptureConfig,
    TurnCaptureStore,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    return TurnCaptureStore(
        TurnCaptureConfig(enabled=True, path=str(tmp_path / "captures.db"))
    )


@pytest.fixture
def provider_config():
    return {
        "providers": {
            "openai": SimpleNamespace(
                base_url="https://api.openai.com",
                chat_path="/v1/chat/completions",
                auth_type="bearer",
                auth_env="OPENAI_API_KEY",
                api_key_header=None,
            ),
        },
        "provider_api_keys": {"openai": "sk-test-key"},
    }


@pytest.fixture
def oauth_config():
    return {
        "providers": {
            "github-copilot": SimpleNamespace(
                base_url="https://api.githubcopilot.com",
                chat_path="/chat/completions",
                auth_type="oauth",
                auth_env=None,
                api_key_header=None,
            ),
        },
        "provider_api_keys": {},
    }


def add_capture(
    store: TurnCaptureStore,
    *,
    request_id: str = "req-123",
    provider: str = "openai",
    pool: str | None = "code",
    request_body: bytes | None = b'{"model":"gpt-4","messages":[]}',
    response_body: bytes | None = b'{"choices":[{"message":{"content":"ok"}}]}',
) -> dict[str, Any]:
    capture_id = store.record(
        request_id=request_id,
        provider=provider,
        model="gpt-4",
        pool=pool,
        request_source="test",
        reason="unusable_tool_response",
        verdict={"tools_requested": True},
        request_payload=request_body,
        response_payload=response_body,
    )
    assert capture_id is not None
    record = store.fetch(capture_id)
    assert record is not None
    return record


async def usable_transport(
    url: str, *, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    return 200, b'{"choices":[{"message":{"content":"ok"}}]}'


def test_selection_by_capture_id_posts_exact_stored_body(store, provider_config):
    request_body = b'{"model":"gpt-4","messages":[{"role":"user","content":"id"}]}'
    record = add_capture(store, request_body=request_body)
    calls: list[dict[str, Any]] = []

    async def fake_transport(url, *, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body})
        return await usable_transport(url, headers=headers, body=body, timeout=timeout)

    assert (
        main(
            ["--capture-id", record["capture_id"]],
            store=store,
            config=provider_config,
            transport=fake_transport,
        )
        == 0
    )
    assert len(calls) == 1
    assert calls[0]["body"] == request_body
    assert calls[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test-key"


def test_selection_by_request_id_uses_newest_matching_capture(store, provider_config):
    older = add_capture(store, request_id="same-request", request_body=b'{"old":true}')
    newest = add_capture(store, request_id="same-request", request_body=b'{"new":true}')
    calls: list[bytes] = []

    async def fake_transport(url, *, headers, body, timeout):
        calls.append(body)
        return await usable_transport(url, headers=headers, body=body, timeout=timeout)

    assert (
        main(
            ["--request-id", "same-request"],
            store=store,
            config=provider_config,
            transport=fake_transport,
        )
        == 0
    )
    assert calls == [newest["request_body"]]
    assert newest["capture_id"] != older["capture_id"]


@pytest.mark.parametrize(
    "kwargs, expected_text",
    [
        (
            {"request_body": None},
            "no request bytes were stored",
        ),
        (
            {"pool": "privacy"},
            "ZDR-excluded",
        ),
        (
            {"pool": None},
            "pool could not be established",
        ),
    ],
)
def test_body_policy_refusals_do_not_send(
    store, provider_config, kwargs, expected_text, capsys
):
    record = add_capture(store, **kwargs)
    calls = 0

    async def no_call_transport(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("HTTP transport must not be called")

    assert (
        main(
            ["--capture-id", record["capture_id"]],
            store=store,
            config=provider_config,
            transport=no_call_transport,
        )
        == 2
    )
    assert calls == 0
    assert expected_text in capsys.readouterr().err


def test_truncated_request_refuses_byte_inexact_replay(tmp_path, monkeypatch, provider_config, capsys):
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    store = TurnCaptureStore(
        TurnCaptureConfig(enabled=True, path=str(tmp_path / "captures.db"), max_request_bytes=8)
    )
    record = add_capture(store, request_body=b"{" + b"x" * 100 + b"}")
    assert record["request_truncated_bytes"] > 0
    calls = 0

    async def no_call_transport(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("HTTP transport must not be called")

    assert (
        main(
            ["--capture-id", record["capture_id"]],
            store=store,
            config=provider_config,
            transport=no_call_transport,
        )
        == 2
    )
    assert calls == 0
    assert "cannot be replayed byte-exactly" in capsys.readouterr().err


def test_not_found_refuses_without_http(store, provider_config, capsys):
    calls = 0

    async def no_call_transport(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("HTTP transport must not be called")

    assert (
        main(
            ["--capture-id", "missing"],
            store=store,
            config=provider_config,
            transport=no_call_transport,
        )
        == 2
    )
    assert calls == 0
    assert "no capture with id" in capsys.readouterr().err


def test_oauth_provider_refuses_without_http(store, oauth_config, capsys):
    record = add_capture(store, provider="github-copilot")
    calls = 0

    async def no_call_transport(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("HTTP transport must not be called")

    assert (
        main(
            ["--capture-id", record["capture_id"]],
            store=store,
            config=oauth_config,
            transport=no_call_transport,
        )
        == 2
    )
    assert calls == 0
    assert "credential pool" in capsys.readouterr().err

def test_local_provider_refuses_without_http(store, capsys):
    local_config = {
        "providers": {
            "claude-code-cli": SimpleNamespace(
                base_url="claude://local",
                chat_path="/chat/completions",
                auth_type="local",
                auth_env=None,
                api_key_header=None,
            ),
        },
        "provider_api_keys": {},
    }
    record = add_capture(store, provider="claude-code-cli")
    calls = 0

    async def no_call_transport(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("HTTP transport must not be called")

    assert (
        main(
            ["--capture-id", record["capture_id"]],
            store=store,
            config=local_config,
            transport=no_call_transport,
        )
        == 2
    )
    assert calls == 0
    assert "local process adapter" in capsys.readouterr().err


def test_tool_markup_as_text_reproduces_unusable_turn(store, provider_config):
    request_body = b'{"model":"gpt-4","messages":[],"tools":[{"function":{"name":"my_tool"}}]}'
    record = add_capture(store, request_body=request_body)

    async def fake_transport(url, *, headers, body, timeout):
        response = b'{"choices":[{"message":{"content":"<tool_call>not a complete call"}}]}'
        return 200, response

    assert (
        main(
            ["--capture-id", record["capture_id"], "--json"],
            store=store,
            config=provider_config,
            transport=fake_transport,
        )
        == 3
    )




def test_streaming_native_tool_call_is_usable(store, provider_config, capsys):
    """A native streamed call clears the captured unusable-tool verdict."""
    record = add_capture(store)
    response = (
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"type":"function","function":{"name":"my_tool","arguments":"{}"}}]},'
        b'"finish_reason":"tool_calls"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    async def fake_transport(url, *, headers, body, timeout):
        return 200, response

    assert (
        main(
            ["--capture-id", record["capture_id"], "--json"],
            store=store,
            config=provider_config,
            transport=fake_transport,
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["replay"]["tool_call_deltas"] == 1
    assert result["replay"]["saw_tool_call"] is True
    assert result["replay"]["usable"] is True
    assert result["replay"]["reason"] is None
    assert result["replay"]["repeat"] == 1
    assert result["replay"]["usable_count"] == 1


def test_repeat_aggregates_usable_count(store, provider_config, capsys):
    """--repeat N re-issues the stored request N times and aggregates results."""
    record = add_capture(store)
    calls: list[int] = []

    async def fake_transport(url, *, headers, body, timeout):
        calls.append(1)
        response = (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            b'"type":"function","function":{"name":"my_tool","arguments":"{}"}}]},'
            b'"finish_reason":"tool_calls"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return 200, response

    assert (
        main(
            [
                "--capture-id",
                record["capture_id"],
                "--repeat",
                "3",
                "--json",
            ],
            store=store,
            config=provider_config,
            transport=fake_transport,
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 3
    assert result["replay"]["repeat"] == 3
    assert result["replay"]["usable_count"] == 3
    assert result["replay"]["usable"] is True
    assert result["replay"]["reason"] is None
    assert len(result["replay"]["runs"]) == 3
    for i, run in enumerate(result["replay"]["runs"], start=1):
        assert run["attempt"] == i
        assert run["usable"] is True


def test_repeat_marks_flaky_when_some_attempts_fail(store, provider_config, capsys):
    """Mixed outcomes yield usable_count < repeat and aggregate_reason='flaky'."""
    record = add_capture(store)
    attempt = {"n": 0}
    good = (
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"type":"function","function":{"name":"my_tool","arguments":"{}"}}]},'
        b'"finish_reason":"tool_calls"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    bad = b'{"choices":[{"message":{"content":""}}]}'

    async def fake_transport(url, *, headers, body, timeout):
        attempt["n"] += 1
        return 200, good if attempt["n"] % 2 == 1 else bad

    assert (
        main(
            [
                "--capture-id",
                record["capture_id"],
                "--repeat",
                "4",
                "--json",
            ],
            store=store,
            config=provider_config,
            transport=fake_transport,
        )
        == 3
    )


def test_repeat_zero_clamps_to_one(store, provider_config, capsys):
    """--repeat 0 must still produce one attempt, not zero (and that attempt is evaluated)."""
    record = add_capture(store)
    calls: list[int] = []
    response = (
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"type":"function","function":{"name":"my_tool","arguments":"{}"}}]},'
        b'"finish_reason":"tool_calls"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    async def fake_transport(url, *, headers, body, timeout):
        calls.append(1)
        return 200, response

    assert (
        main(
            [
                "--capture-id",
                record["capture_id"],
                "--repeat",
                "0",
                "--json",
            ],
            store=store,
            config=provider_config,
            transport=fake_transport,
        )
        == 0
    )
    assert len(calls) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["replay"]["repeat"] == 1
    assert result["replay"]["usable_count"] == 1


def test_repeat_persists_replay_runs_to_capture(store, provider_config, capsys):
    """--repeat N appends a single ``replay_runs`` entry to the capture record."""
    record = add_capture(store)
    assert store.fetch(record["capture_id"])["replay_runs"] == []

    async def fake_transport(url, *, headers, body, timeout):
        return 200, (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            b'"type":"function","function":{"name":"my_tool","arguments":"{}"}}]},'
            b'"finish_reason":"tool_calls"}]}\n\n'
            b"data: [DONE]\n\n"
        )

    assert (
        main(
            [
                "--capture-id",
                record["capture_id"],
                "--repeat",
                "3",
                "--json",
            ],
            store=store,
            config=provider_config,
            transport=fake_transport,
        )
        == 0
    )
    # One replay_runs entry per main() invocation, not per attempt.
    updated = store.fetch(record["capture_id"])
    assert len(updated["replay_runs"]) == 1
    run = updated["replay_runs"][0]
    assert run["repeat"] == 3
    assert run["usable_count"] == 3
    assert run["provider"] == "openai"
    assert len(run["runs"]) == 3
    assert [r["attempt"] for r in run["runs"]] == [1, 2, 3]


def test_single_repeat_does_not_persist_replay_runs(store, provider_config, capsys):
    """Without --repeat > 1, no replay_runs entry is written."""
    record = add_capture(store)

    async def fake_transport(url, *, headers, body, timeout):
        return 200, (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            b'"type":"function","function":{"name":"my_tool","arguments":"{}"}}]},'
            b'"finish_reason":"tool_calls"}]}\n\n'
            b"data: [DONE]\n\n"
        )

    assert (
        main(
            ["--capture-id", record["capture_id"], "--json"],
            store=store,
            config=provider_config,
            transport=fake_transport,
        )
        == 0
    )
    assert store.fetch(record["capture_id"])["replay_runs"] == []




def test_list_prints_capture_metadata_table(store, capsys):
    add_capture(store)
    assert main(["--list"], store=store, config={}) == 0
    output = capsys.readouterr().out
    assert "capture_id" in output
    assert "provider/model" in output
    assert "body_policy" in output
    assert "request_bytes" not in output



class _StubProvider(BaseHTTPRequestHandler):
    """A local provider that records what the replay tool actually sent."""

    protocol_version = "HTTP/1.1"
    received: list[tuple[str, str | None, bytes]] = []
    response_body = b"data: [DONE]\n\n"

    def do_POST(self):  # noqa: N802 - stdlib handler name
        length = int(self.headers.get("Content-Length") or 0)
        type(self).received.append(
            (self.path, self.headers.get("Authorization"), self.rfile.read(length))
        )
        body = type(self).response_body
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Keep the real HTTP exchange out of the captured test output."""


@pytest.fixture
def stub_provider():
    """Serve a throwaway provider on a local port."""
    _StubProvider.received = []
    server = HTTPServer(("127.0.0.1", 0), _StubProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_resolves_policy_and_posts_stored_bytes(
    tmp_path, monkeypatch, stub_provider, capsys
):
    """The operator path: env policy, real config, real HTTP, exact bytes.

    The stub is what makes this more than a re-test of the injected transport:
    the CLI resolves the capture store and the provider route from the process
    environment exactly as it does for an operator, then POSTs through aiohttp.
    """
    _StubProvider.response_body = (
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"type":"function","function":{"name":"my_tool","arguments":"{}"}}]},'
        b'"finish_reason":"tool_calls"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    monkeypatch.setenv("TUSKER_STATE_DATABASE_URL", "")
    monkeypatch.setenv("TUSKER_STREAM_CAPTURE", "1")
    monkeypatch.setenv("TUSKER_TURN_CAPTURE_PATH", str(tmp_path / "captures.db"))
    monkeypatch.setenv("PROVIDER_SMOKE_API_KEY", "smoke-token")
    monkeypatch.setenv(
        "PROVIDER_REGISTRY_JSON",
        json.dumps(
            {
                "smoke": {
                    "kind": "bearer",
                    "base_url": stub_provider,
                    "chat_path": "/v1/chat/completions",
                    "auth_env": "PROVIDER_SMOKE_API_KEY",
                }
            }
        ),
    )
    import tusker_gateway.turn_capture as turn_capture

    monkeypatch.setattr(turn_capture, "_STORE", None)
    monkeypatch.setattr(turn_capture, "_STORE_CONFIG", None)
    request_body = b'{"model":"smoke-model","stream":true,"tools":[]}'
    store = turn_capture.capture_store()
    assert store is not None
    record = add_capture(store, provider="smoke", request_body=request_body)

    # No store or config arguments: both come from the environment.
    assert main(["--capture-id", record["capture_id"], "--json"]) == 0

    assert len(_StubProvider.received) == 1
    path, authorization, sent = _StubProvider.received[0]
    assert path == "/v1/chat/completions"
    assert authorization == "Bearer smoke-token"
    assert sent == request_body
    result = json.loads(capsys.readouterr().out)
    assert result["replay"]["usable"] is True
    assert result["replay"]["saw_tool_call"] is True
