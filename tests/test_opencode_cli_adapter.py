from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from tusker_gateway.config import DEFAULT_PROVIDER_REGISTRY
from tusker_gateway.errors import (
    BadRequestError,
    ProviderAuthError,
    ProviderError,
    ProviderModelRetiredError,
    ProviderRouteDisabledError,
    RateLimitError,
)
from tusker_gateway.provider_adapters import provider_adapters
from tusker_gateway.provider_adapters.opencode_cli import (
    OpenCodeCLIAdapter,
    _cli_failure_error,
    _model_for_cli,
)


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = 34567

    async def communicate(self, _input: bytes):
        return self.stdout, self.stderr

    async def wait(self):
        return self.returncode

class _FakeStreamingProcess(_FakeProcess):
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        super().__init__(stdout, stderr, returncode)
        self.stdin = self
        self.stdout = self
        self.stderr = self
        self._lines = list(stdout.splitlines(keepends=True)) + [b""]

    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self) -> bytes:
        return b""


def _tool(name: str = "report_value"):
    return {"type": "function", "function": {
        "name": name,
        "description": "Return a value to the caller without executing it.",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
    }}


def test_opencode_cli_is_registered_as_non_zdr_local_provider():
    assert provider_adapters.get("opencode-cli") is not None
    provider = DEFAULT_PROVIDER_REGISTRY["opencode-cli"]
    assert provider.kind == "local"
    assert provider.zdr_ok is False


@pytest.mark.parametrize("value,expected", [
    ("big-pickle", "opencode/big-pickle"),
    ("opencode/big-pickle", "opencode/big-pickle"),
])
def test_model_name_maps_only_to_opencode_catalog(value, expected):
    assert _model_for_cli(value) == expected


@pytest.mark.parametrize("value", ["", "other-provider/model", "opencode/../model", "opencode/a/b", "-bad"])
def test_model_name_rejects_other_providers_and_invalid_identifiers(value):
    with pytest.raises(BadRequestError):
        _model_for_cli(value)


@pytest.mark.asyncio
async def test_route_is_opt_in(monkeypatch):
    monkeypatch.delenv("TUSKER_OPENCODE_CLI_ENABLED", raising=False)
    with pytest.raises(ProviderRouteDisabledError):
        await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle", messages=[],
        )


@pytest.mark.asyncio
async def test_text_result_parses_opencode_json_event_stream(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "test-zen-key")
    stdout = b'\n'.join([
        json.dumps({"type": "step_start", "part": {}}).encode(),
        json.dumps({"type": "text", "part": {"text": "hello "}}).encode(),
        json.dumps({"type": "text", "part": {"text": "world"}}).encode(),
    ])
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="/opt/opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(stdout))) as spawn:
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle", messages=[{"role": "user", "content": "hi"}],
        )
    assert result["choices"][0]["message"]["content"] == "hello world"
    args = spawn.await_args.args
    assert args[0] == "/opt/opencode"
    assert args[args.index("--model") + 1] == "opencode/big-pickle"
    assert "--standalone" in args
    env = spawn.await_args.kwargs["env"]
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "true"
    assert "OPENCODE_API_KEY" not in env
    assert "OPENCODE_ZEN_API_KEY" not in env
    assert "OPENCODE_CONFIG_CONTENT" not in env
    assert spawn.await_args.kwargs["cwd"]


@pytest.mark.asyncio
async def test_opencode_stream_uses_incremental_json_event_reader(monkeypatch):
    from tusker_gateway.provider_adapters import cli_streaming

    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    marker = object()
    captured = {}

    def stream(command, **kwargs):
        captured.update(command=command, **kwargs)
        return marker

    monkeypatch.setattr(cli_streaming, "stream_cli_jsonl", stream)
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="/opt/opencode"):
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle",
            messages=[{"role": "user", "content": "hello"}], stream=True,
        )
    assert result is marker
    assert captured["command"] == [
        "/opt/opencode", "run", "--standalone", "--format", "json",
        "--model", "opencode/big-pickle",
    ]
    assert captured["text_extractor"].__name__ == "text_from_event"

@pytest.mark.asyncio
async def test_opencode_stream_surfaces_model_retirement(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    event = {"type": "error", "error": {"message": "Invalid model reference: old-free"}}
    process = _FakeStreamingProcess(json.dumps(event).encode() + b"\n")
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="old-free", messages=[], stream=True,
        )
        with pytest.raises(ProviderModelRetiredError):
            _ = [frame async for frame in result]


@pytest.mark.asyncio
async def test_explicit_opencode_api_key_is_forwarded_without_aliasing_zen_key(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    monkeypatch.setenv("OPENCODE_API_KEY", "explicit-cli-key")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "gateway-provider-key")
    stdout = json.dumps({"type": "text", "part": {"text": "ok"}}).encode()
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="/opt/opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(stdout))) as spawn:
        await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle", messages=[{"role": "user", "content": "hi"}],
        )
    env = spawn.await_args.kwargs["env"]
    assert env["OPENCODE_API_KEY"] == "explicit-cli-key"
    assert "OPENCODE_ZEN_API_KEY" not in env


@pytest.mark.asyncio
async def test_deployment_scoped_zen_key_is_forwarded_only_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_API_KEY", "worker-scoped-key")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "unaliased-provider-key")
    stdout = json.dumps({"type": "text", "part": {"text": "ok"}}).encode()
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="/opt/opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(stdout))) as spawn:
        await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle", messages=[{"role": "user", "content": "hi"}],
        )
    env = spawn.await_args.kwargs["env"]
    assert env["OPENCODE_API_KEY"] == "worker-scoped-key"
    assert "TUSKER_OPENCODE_CLI_API_KEY" not in env
    assert "OPENCODE_ZEN_API_KEY" not in env


@pytest.mark.asyncio
async def test_mcp_tool_invocation_becomes_openai_tool_call(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "1")

    class ToolCallProcess(_FakeProcess):
        async def communicate(self, _input: bytes):
            env = json.loads(spawn.await_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
            bridge = env["mcp"]["gateway"]["environment"]
            Path(bridge["TUSKER_MCP_CALL_FILE"]).write_text(json.dumps({
                "id": "call_opencode_1", "name": "report_value", "arguments": {"value": "ok"},
            }))
            return b"", b""

    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=ToolCallProcess(b""))) as spawn:
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle", messages=[{"role": "user", "content": "report"}],
            tools=[_tool()], tool_choice="required",
        )

    call = result["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_opencode_1"
    assert call["function"]["name"] == "report_value"
    assert json.loads(call["function"]["arguments"]) == {"value": "ok"}
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    config = json.loads(spawn.await_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
    assert "permission" not in config
    assert config["mcp"]["gateway"]["type"] == "local"


@pytest.mark.asyncio
async def test_cli_error_and_missing_binary_are_actionable(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    adapter = OpenCodeCLIAdapter()
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value=None):
        with pytest.raises(ProviderError) as exc:
            await adapter.chat(provider="opencode-cli", model="big-pickle", messages=[])
    assert exc.value.code == "opencode_cli_unavailable"

    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(b"not json"))):
        with pytest.raises(ProviderError) as exc:
            await adapter.chat(provider="opencode-cli", model="big-pickle", messages=[])
    assert exc.value.code == "invalid_upstream_response"


@pytest.mark.asyncio
async def test_nonzero_exit_logs_bounded_stderr_and_stdout(monkeypatch):
    """A non-zero exit must leave a diagnostic trace.

    Production evidence, req_d021ef863a033d52 (2026-10-21): four
    opencode-cli candidates exited rc=1 with stderr_bytes=0, and stderr was
    the only field logged, so the failure left nothing to diagnose. stdout
    is bounded too - an unbounded preview is its own log-volume bug.
    """
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    stderr = b"opencode: fatal auth error\n" + b"x" * 600
    stdout = b"y" * 4000
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(stdout, stderr=stderr, returncode=1))), \
         patch("tusker_gateway.provider_adapters.opencode_cli.logger") as log:
        with pytest.raises(ProviderError) as exc:
            await OpenCodeCLIAdapter().chat(provider="opencode-cli", model="big-pickle", messages=[])

    assert exc.value.code == "opencode_cli_failed"
    (message, rc, size, preview, out), _ = log.warning.call_args
    assert message == "opencode-cli exited rc=%s stderr_bytes=%d stderr=%r stdout=%r"
    assert (rc, size) == (1, len(stderr))
    assert preview == stderr.decode("utf-8")[:512]
    assert len(preview) == 512
    assert len(out) == 1024
    assert out == stdout.decode("utf-8")[:1024]


@pytest.mark.asyncio
async def test_deprecated_model_exit_marks_route_retired(monkeypatch):
    """Deterministic CLI model rejections carry upstream 410 so the
    permanent-failure marker prunes the route; other exits stay generic."""
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    cases = [
        # Legacy text-mode rejections land on stderr.
        (b"", b"Error: Model exo-free has been deprecated.\n", True),
        (b"", b"Error: Upstream request failed: Model is unavailable.\n", True),
        # --format json reports model-level rejections as a stdout event.
        (b'{"type":"error","error":{"type":"unknown",'
         b'"message":"Invalid model reference: exo-free"}}\n', b"", True),
        (b"opencode: transient network failure\n", b"", False),
    ]
    for stdout, stderr, retired in cases:
        with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="opencode"), \
             patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=_FakeProcess(stdout, stderr=stderr, returncode=1))):
            with pytest.raises(ProviderError) as exc:
                await OpenCodeCLIAdapter().chat(
                    provider="opencode-cli", model="exo-free", messages=[]
                )
        assert exc.value.status == 502  # client-visible status unchanged
        if retired:
            assert exc.value.code == "model_retired"
            assert exc.value.upstream_status == 410
        else:
            assert exc.value.code == "opencode_cli_failed"
            assert getattr(exc.value, "upstream_status", None) is None



@pytest.mark.asyncio
async def test_opencode_forwards_base64_image_as_file(monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    stdout = json.dumps({"type": "text", "part": {"text": "seen"}}).encode()
    captured: dict[str, bytes] = {}
    async def _spawn(*args, **kwargs):
        image_path = args[args.index("--file") + 1]
        captured["image"] = Path(image_path).read_bytes()
        return _FakeProcess(stdout)
    with patch("tusker_gateway.provider_adapters.opencode_cli.shutil.which", return_value="opencode"), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec", new=_spawn):
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}},
            ]}], stream=False,
        )
    assert result["choices"][0]["message"]["content"] == "seen"
    assert captured["image"] == b"hi"


class _FakeStdin:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeStderr:
    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    async def read(self) -> bytes:
        return self.data


class _FakeWarmRun:
    """Stand-in for a warm `opencode run` child: JSON events on stdout, then EOF.

    It reproduces the contract the gateway depends on: a tool call is published
    by writing the broker pointer's call file while the model keeps generating,
    so it must be detected without waiting for the child to exit.
    """

    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        pointer: Path | None = None,
        call_payload: dict[str, Any] | None = None,
        returncode: int = 0,
    ) -> None:
        self._lines = [json.dumps(event).encode() + b"\n" for event in events]
        self._pointer = pointer
        self._call_payload = call_payload
        self._call_written = False
        self.stdin = _FakeStdin()
        self.stdout = self
        self.stderr = _FakeStderr()
        self.returncode: int | None = None
        self._exit_code = returncode
        self.pid = 4242

    async def readline(self) -> bytes:
        if self._pointer is not None and not self._lines and not self._call_written:
            published = json.loads(self._pointer.read_text(encoding="utf-8"))
            Path(published["call"]).write_text(json.dumps(self._call_payload), encoding="utf-8")
            self._call_written = True
            await asyncio.sleep(60)  # the request's poll timeout cancels this
        if self._lines:
            return self._lines.pop(0)
        return b""

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code


def _warm_control_recorder(url: str = "http://127.0.0.1:4096"):
    """Fake `_run_cli_tool`: records (args, env) and reports the warm service."""
    calls: list[tuple[list[str], dict[str, str]]] = []

    async def run(binary: str, args: list[str], env: dict[str, str], *, timeout: float = 30.0, cwd=None):
        calls.append((list(args), dict(env)))
        if args[:2] in (["service", "status"], ["service", "start"]):
            return 0, f"opencode server listening on {url}\n".encode(), b""
        return 0, b"", b""

    return calls, run


@pytest.fixture
def warm_state(monkeypatch, tmp_path):
    """Isolate warm module state, the broker directory, and warm env flags.

    Executable discovery is mocked alongside the control-plane calls so the
    warm-service contract is tested hermetically: these tests must pass on
    machines without an ``opencode`` binary installed.
    """
    from tusker_gateway.provider_adapters import opencode_cli as module

    monkeypatch.setenv("TUSKER_OPENCODE_CLI_ENABLED", "true")
    monkeypatch.setenv("TUSKER_OPENCODE_WARM_ENABLED", "1")
    monkeypatch.setenv("TUSKER_OPENCODE_WARM_BROKER_DIR", str(tmp_path / "broker"))
    monkeypatch.setenv("TUSKER_OPENCODE_WARM_IDLE_SECS", "0")
    # chat() resolves the executable through shutil.which before the warm
    # path runs; without this patch the seven warm-service tests demand a
    # real `opencode` binary even though all warm behaviour is faked below.
    monkeypatch.setattr(
        module.shutil, "which", lambda executable: f"/opt/fake/{executable}"
    )
    monkeypatch.setattr(module, "_WARM_SERVICE_URL", None)
    monkeypatch.setattr(module, "_WARM_LAST_USE", 0.0)
    monkeypatch.setattr(module, "_WARM_IDLE_TASK", None)
    return module


def _broker_path() -> Path:
    return Path(os.environ["TUSKER_OPENCODE_WARM_BROKER_DIR"])


def _warm_patches(run, process):
    return (
        patch("tusker_gateway.provider_adapters.opencode_cli._run_cli_tool", new=run),
        patch(
            "tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    )


@pytest.mark.asyncio
async def test_warm_service_serves_text_request_without_standalone(warm_state):
    calls, run = _warm_control_recorder()
    fake = _FakeWarmRun([
        {"type": "step_start", "part": {}, "sessionID": "ses_warm_text"},
        {"type": "text", "part": {"text": "warm "}},
        {"type": "text", "part": {"text": "answer"}},
    ])
    controls, spawn_patch = _warm_patches(run, fake)
    with controls, spawn_patch as spawn:
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert result["choices"][0]["message"]["content"] == "warm answer"
    assert result["choices"][0]["finish_reason"] == "stop"
    args = spawn.await_args.args
    assert "--standalone" not in args
    assert args[args.index("--model") + 1] == "opencode/big-pickle"
    # Tools reach the shared server through the broker pointer, not per-request
    # config, which the warm service read once at start.
    assert "OPENCODE_CONFIG_CONTENT" not in spawn.await_args.kwargs["env"]
    assert fake.stdin.written.decode() != ""
    started = next(env for args, env in calls if args[:2] == ["service", "start"])
    bridge = json.loads(started["OPENCODE_CONFIG_CONTENT"])["mcp"]["gateway"]
    assert bridge["environment"]["TUSKER_MCP_BROKER_DIR"] == str(_broker_path())
    assert ["api", "DELETE", "/api/session/ses_warm_text"] in [args for args, _ in calls]
    assert (_broker_path() / "active.json").read_text(encoding="utf-8") == "{}"


@pytest.mark.asyncio
async def test_warm_stream_emits_each_event_as_its_own_frame(warm_state):
    _calls, run = _warm_control_recorder()
    fake = _FakeWarmRun([
        {"type": "text", "part": {"text": "first "}, "sessionID": "ses_warm_stream"},
        {"type": "text", "part": {"text": "second"}},
    ])
    controls, spawn_patch = _warm_patches(run, fake)
    with controls, spawn_patch:
        stream = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle",
            messages=[{"role": "user", "content": "hi"}], stream=True,
        )
        frames = [frame async for frame in stream]

    payloads = [json.loads(frame[len(b"data: "):]) for frame in frames[:-1]]
    contents = [payload["choices"][0]["delta"].get("content") for payload in payloads]
    assert contents[:2] == ["first ", "second"]
    assert len({payload["id"] for payload in payloads}) == 1
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert frames[-1] == b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_warm_tool_call_interrupts_and_reports_tool_call(warm_state):
    calls, run = _warm_control_recorder()
    fake = _FakeWarmRun(
        [{"type": "step_start", "part": {}, "sessionID": "ses_warm_tool"}],
        pointer=_broker_path() / "active.json",
        call_payload={"id": "call_warm_1", "name": "report_value", "arguments": {"value": "ok"}},
    )
    controls, spawn_patch = _warm_patches(run, fake)
    with controls, \
         patch("tusker_gateway.provider_adapters.opencode_cli._stop_process", new=AsyncMock()) as stop, \
         spawn_patch as spawn:
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle",
            messages=[{"role": "user", "content": "report"}],
            tools=[_tool()], tool_choice="required",
        )

    call = result["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "report_value"
    assert json.loads(call["function"]["arguments"]) == {"value": "ok"}
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    commands = [args for args, _ in calls]
    assert commands.index(["api", "POST", "/api/session/ses_warm_tool/interrupt"]) < \
        commands.index(["api", "DELETE", "/api/session/ses_warm_tool"])
    # Stopping the child is idempotent, so an extra close-time stop is harmless.
    assert stop.await_count >= 1
    assert "--standalone" not in spawn.await_args.args
    assert (_broker_path() / "active.json").read_text(encoding="utf-8") == "{}"


@pytest.mark.asyncio
async def test_closing_the_stream_stops_warm_generation(warm_state):
    calls, run = _warm_control_recorder()
    fake = _FakeWarmRun([{"type": "text", "part": {"text": "partial"}, "sessionID": "ses_close"}])
    controls, spawn_patch = _warm_patches(run, fake)
    with controls, \
         patch("tusker_gateway.provider_adapters.opencode_cli._stop_process", new=AsyncMock()) as stop, \
         spawn_patch:
        stream = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle",
            messages=[{"role": "user", "content": "hi"}], stream=True,
        )
        assert b"partial" in await stream.__anext__()
        await stream.aclose()

    assert stop.await_count == 1
    assert ["api", "DELETE", "/api/session/ses_close"] in [args for args, _ in calls]
    assert (_broker_path() / "active.json").read_text(encoding="utf-8") == "{}"


@pytest.mark.asyncio
async def test_warm_service_is_reused_across_requests(warm_state):
    calls, run = _warm_control_recorder()

    def new_process(*_args, **_kwargs):
        return _FakeWarmRun([{"type": "text", "part": {"text": "again"}}])

    controls = patch("tusker_gateway.provider_adapters.opencode_cli._run_cli_tool", new=run)
    spawn_patch = patch(
        "tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=new_process),
    )
    with controls, spawn_patch as spawn:
        adapter = OpenCodeCLIAdapter()
        for _ in range(2):
            result = await adapter.chat(
                provider="opencode-cli", model="big-pickle",
                messages=[{"role": "user", "content": "hi"}],
            )
            assert result["choices"][0]["message"]["content"] == "again"

    commands = [args for args, _ in calls]
    assert commands.count(["service", "start"]) == 1
    assert commands.count(["service", "status"]) == 1
    assert spawn.await_count == 2


@pytest.mark.asyncio
async def test_foreign_service_is_replaced_to_keep_the_broker_contract(warm_state, monkeypatch):
    monkeypatch.setattr(warm_state, "_WARM_SERVICE_URL", "http://127.0.0.1:4096")
    calls, run = _warm_control_recorder(url="http://127.0.0.1:5000")
    with patch("tusker_gateway.provider_adapters.opencode_cli._run_cli_tool", new=run):
        url = await warm_state._ensure_warm_service("opencode", {"PATH": "/usr/bin"})

    assert url == "http://127.0.0.1:5000"
    assert [args[:2] for args, _ in calls] == [
        ["service", "status"], ["service", "stop"], ["service", "start"],
    ]
    assert warm_state._WARM_SERVICE_URL == "http://127.0.0.1:5000"
    assert calls[-1][1]["OPENCODE_CONFIG_CONTENT"]


@pytest.mark.asyncio
async def test_warm_idle_watchdog_stops_the_service(warm_state, monkeypatch):
    monkeypatch.setattr(warm_state, "_WARM_SERVICE_URL", "http://127.0.0.1:4096")
    calls, run = _warm_control_recorder()
    with patch("tusker_gateway.provider_adapters.opencode_cli._run_cli_tool", new=run):
        await warm_state._warm_idle_watchdog("opencode", {"PATH": "/usr/bin"}, 0.01)

    assert [args for args, _ in calls] == [["service", "stop"]]
    assert warm_state._WARM_SERVICE_URL is None


@pytest.mark.asyncio
async def test_warm_service_failure_falls_back_to_isolated_cli(warm_state):
    async def failing(binary: str, args: list[str], env: dict[str, str], *, timeout: float = 30.0):
        return 1, b"", b"service unavailable"

    stdout = json.dumps({"type": "text", "part": {"text": "standalone"}}).encode() + b"\n"
    with patch("tusker_gateway.provider_adapters.opencode_cli._run_cli_tool", new=failing), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(stdout))) as spawn:
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert result["choices"][0]["message"]["content"] == "standalone"
    assert "--standalone" in spawn.await_args.args


@pytest.mark.asyncio
async def test_disabled_warm_path_leaves_the_service_untouched(warm_state, monkeypatch):
    monkeypatch.setenv("TUSKER_OPENCODE_WARM_ENABLED", "0")
    calls, run = _warm_control_recorder()
    stdout = json.dumps({"type": "text", "part": {"text": "standalone"}}).encode() + b"\n"
    with patch("tusker_gateway.provider_adapters.opencode_cli._run_cli_tool", new=run), \
         patch("tusker_gateway.provider_adapters.opencode_cli.asyncio.create_subprocess_exec",
               new=AsyncMock(return_value=_FakeProcess(stdout))) as spawn:
        result = await OpenCodeCLIAdapter().chat(
            provider="opencode-cli", model="big-pickle",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert result["choices"][0]["message"]["content"] == "standalone"
    assert calls == []
    assert "--standalone" in spawn.await_args.args



@pytest.mark.parametrize("diagnostics", [
    b'{"error":{"statusCode":429,"message":"Token limit is exceeded. Try again in 8 seconds."}}',
    b"opencode error: HTTP 429 Too Many Requests",
    b"Error: 429 too many requests, retry later",
    b"rate limit exceeded, try again",
    b"Rate-Limit hit for model big-pickle",
    b"rate_limited: please retry",
])
def test_cli_rate_limit_diagnostics_map_to_rate_limit_error(diagnostics):
    error = _cli_failure_error(diagnostics)
    assert isinstance(error, RateLimitError)


@pytest.mark.parametrize("diagnostics", [
    # A quota that is merely mentioned, not exhausted.
    b"model quota: 1.2M tokens/day remaining",
    # Digits that happen to contain 429 must not be read as a status.
    b"exit code 429 references line 12 of config",
    b"session 4291 aborted",
    b"token count 429021 exceeds context",
    # The real shape that reached the client on req_d021ef863a033d52.
    b"Error: invalid tool call arguments",
    b"",
])
def test_unrelated_cli_failures_stay_generic(diagnostics):
    error = _cli_failure_error(diagnostics)
    assert not isinstance(error, RateLimitError)
    assert error.code == "opencode_cli_failed"


@pytest.mark.parametrize("diagnostics", [
    # The exact stdout event that broke req_d021ef863a033d52 (2026-10-21):
    # the CLI reports auth failures on stdout, so stderr stayed empty.
    b'{"type":"error","error":{"type":"provider.auth","status":401,'
    b'"message":"This API key has expired. Create a new API key in OpenCode Console."}}',
    b"HTTP 401 Unauthorized",
    b"error: 401 Unauthorized",
    b'{"error":{"type":"authentication_error","message":"Invalid API key"}}',
])
def test_cli_auth_diagnostics_map_to_provider_auth_error(diagnostics):
    error = _cli_failure_error(diagnostics)
    assert isinstance(error, ProviderAuthError)
    # Carrying upstream 401 is what earns the permanent cooldown; without it
    # the dead credential re-probed on the transient ladder indefinitely.
    assert error.upstream_status == 401


@pytest.mark.parametrize("diagnostics", [
    b"Error: invalid tool call arguments",
    b"model quota: 1.2M tokens/day remaining",
    b"step 14012 completed",
    b"token count 4012 exceeded",
    b"session 4015 aborted",
])
def test_digits_that_merely_contain_401_are_not_auth(diagnostics):
    assert not isinstance(_cli_failure_error(diagnostics), ProviderAuthError)
