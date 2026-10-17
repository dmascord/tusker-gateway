"""Capture sanitized golden sessions from real clients.

Runbook Phase 2.2: record representative request shapes from OMP, OpenCode,
Cline and plain OpenAI-SDK clients, then strip secrets, PII and reasoning text
while keeping protocol structure and event ordering.

The tool stands up a loopback proxy that answers chat-completions requests
with a **scripted** SSE stream (a reasoning chunk, content, then a tool call
on the first turn; plain text afterwards). Because the client believes the
model called a tool, a cooperating agent CLI follows up with a second request
that carries the assistant ``tool_calls`` and the ``tool`` result — so the
capture covers the continuation shape that matters most, from a real client
rather than a hand-written fixture.

Nothing leaves the machine: the proxy answers locally, and no upstream
credential is read. The raw recording is sanitized before it is written:

- ``Authorization`` / cookie / session headers are dropped entirely.
- Header *names* are kept (they are part of the client contract), values are
  reduced to a flag per sensitive key.
- Prompt, tool-argument and tool-result *text* is replaced with placeholders;
  message roles, ordering, ``tool_calls`` structure, ids and field names are
  preserved.
- ``reasoning`` / ``reasoning_content`` deltas are reduced to a placeholder
  token, so the *fact* that reasoning precedes content is preserved while the
  text itself never reaches the fixture.
- Absolute paths, hostnames, emails and long hex/base64 runs are replaced.

Usage::

    # 1. Proxy + sanitizer (stops itself once the continuation lands).
    python3 -m tusker_gateway.tools.capture_golden_sessions \\
        --client opencode --tool-name glob

    # 2. Drive the real client at the printed base URL. OpenCode only
    #    honours a project-level opencode.json (an OPENCODE_CONFIG env var
    #    is ignored), so run it from a scratch directory holding one:
    #      {"provider": {"capture": {"npm": "@ai-sdk/openai-compatible",
    #        "options": {"baseURL": "http://127.0.0.1:8941/v1",
    #                    "apiKey": "sk-capture-probe"},
    #        "models": {"capture/capture-model": {"name": "Capture"}}}}}
    opencode run --standalone --model capture/capture-model "list the files"

    # A plain OpenAI SDK works the same way: point base_url at the proxy,
    # stream a chat with ``tools=[glob]``, then send the result back as an
    # assistant tool-call message followed by a ``tool`` message.

Output lands in ``tests/fixtures/golden/<client>.json``. Run with ``--check``
to re-validate committed fixtures instead of capturing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "golden"

# Headers whose *values* never reach a fixture. Names are kept so the replay
# harness can assert the gateway tolerates them.
SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "proxy-authorization",
    "x-api-key", "api-key", "x-auth-token", "x-session-affinity",
    "traceparent", "b3", "x-opencode-project", "x-opencode-session",
    "x-opencode-session-id", "x-session-id", "x-omp-session-id",
    "x-conversation-id",
}
# Header names kept verbatim (they identify the client without being secret).
CLIENT_HEADER_ALLOW = {
    "user-agent", "content-type", "accept", "x-opencode-client",
    "x-tusker-harness", "x-client-harness", "anthropic-version",
    "accept-encoding",
}

_PLACEHOLDER = "<redacted>"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{24,}\b")
# Absolute filesystem paths (POSIX and Windows) leak usernames and layout.
_PATH_RE = re.compile(r"(?:/Users|/home|/srv|/mnt|/var|/opt|/tmp|[A-Za-z]:\\)[^\s\"']*")
_HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:net|au|com|io|local|internal)\b")


def sanitize_text(value: str, *, keep: int = 0) -> str:
    """Replace private content, preserving only structure and a bounded head.

    ``keep`` retains that many leading characters of the *shape* the client
    sent (useful when the request framing itself is what matters), otherwise
    the whole string collapses to a placeholder.
    """
    if not value:
        return value
    if keep and len(value) <= keep:
        text = value
    else:
        text = _PLACEHOLDER
    text = _EMAIL_RE.sub("<email>", text)
    text = _PATH_RE.sub("<path>", text)
    text = _HOST_RE.sub("<host>", text)
    return _HEX_RE.sub("<hex>", text)


def sanitize_headers(headers: dict[str, str]) -> dict[str, Any]:
    """Keep client-identifying header names; flag the rest."""
    out: dict[str, Any] = {}
    for name, value in headers.items():
        low = name.lower()
        if low in CLIENT_HEADER_ALLOW:
            out[low] = sanitize_text(value, keep=200)
        elif low in SENSITIVE_HEADERS:
            out[low] = "<redacted>"
        else:
            # Unknown header: record presence only, never the value.
            out[low] = "<present>"
    return out


def sanitize_message(message: dict[str, Any]) -> dict[str, Any]:
    """Preserve role order, tool-call structure and ids; drop all text."""
    out: dict[str, Any] = {}
    for key in ("role", "name", "tool_call_id", "id", "type"):
        if key in message:
            out[key] = message[key]
    if "content" in message:
        content = message["content"]
        if isinstance(content, str):
            out["content"] = sanitize_text(content)
        elif isinstance(content, list):
            out["content"] = [
                {
                    "type": block.get("type"),
                    **( {"text": sanitize_text(block.get("text", ""))}
                        if "text" in block else {}),
                }
                for block in content
                if isinstance(block, dict)
            ]
        else:
            out["content"] = sanitize_text(str(content))
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        out["tool_calls"] = [
            {
                "id": call.get("id"),
                "type": call.get("type"),
                "function": {
                    "name": (call.get("function") or {}).get("name"),
                    "arguments": sanitize_text(
                        (call.get("function") or {}).get("arguments", "")
                    ),
                },
            }
            for call in calls
            if isinstance(call, dict)
        ]
    return out


def sanitize_tools(tools: Any) -> list[dict[str, Any]]:
    """Tool *schemas* are protocol structure; descriptions are not content."""
    if not isinstance(tools, list):
        return []
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        params = fn.get("parameters") or {}
        out.append({
            "type": tool.get("type"),
            "function": {
                "name": fn.get("name"),
                "parameters": {
                    "type": params.get("type"),
                    "properties": sorted((params.get("properties") or {}).keys()),
                    "required": sorted(params.get("required") or []),
                },
            },
        })
    return out


def sanitize_request(
    path: str, headers: dict[str, str], body: dict[str, Any],
) -> dict[str, Any]:
    """One captured request reduced to its protocol structure."""
    record: dict[str, Any] = {
        "path": path,
        "headers": sanitize_headers(headers),
        "message_roles": [
            m.get("role") for m in (body.get("messages") or []) if isinstance(m, dict)
        ],
        "messages": [
            sanitize_message(m) for m in (body.get("messages") or [])
            if isinstance(m, dict)
        ],
        "tools": sanitize_tools(body.get("tools")),
        # Field *presence* is the part of the wire contract under test.
        "body_keys": sorted(body.keys()),
        "stream": bool(body.get("stream")),
    }
    for key in ("store", "stream_options", "max_completion_tokens",
                "max_tokens", "temperature", "top_p", "model",
                "tool_choice", "parallel_tool_calls", "prompt_cache_key"):
        if key in body:
            value = body[key]
            record[key] = sanitize_text(value, keep=64) if isinstance(value, str) else value
    return record



def _is_continuation(body: dict[str, Any]) -> bool:
    """True when the request carries an assistant tool call plus a result.

    That is the tool-result continuation shape: the client ran the tool and
    is sending the result back so the model can finish the turn.
    """
    messages = body.get("messages") or []
    has_call = any(
        isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
        for m in messages
    )
    has_result = any(
        isinstance(m, dict) and m.get("role") == "tool" for m in messages
    )
    return has_call and has_result
# ─── scripted upstream ──────────────────────────────────────────────────────

def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    return _sse({
        "id": "chatcmpl-golden", "object": "chat.completion.chunk",
        "model": "capture-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    })


class CaptureProxy:
    """Loopback proxy: scripts a tool-calling turn, records client requests."""

    def __init__(self, *, tool_name: str = "read_file"):
        self.requests: list[dict[str, Any]] = []
        self.tool_name = tool_name
        self._tool_call_served = False
        self.done = asyncio.Event()

    def _frames(self, body: dict[str, Any]) -> list[bytes]:
        """Call the scripted tool once, then answer with plain text.

        Some agents make a lightweight no-tools call (title generation)
        before the real run; serving the tool call to *that* wastes the
        only scripted turn. Wait for the request that actually declares
        the tool, so the call lands where the agent will follow up with a
        tool-result continuation.
        """
        tool_names = {
            (t.get("function") or {}).get("name")
            for t in (body.get("tools") or []) if isinstance(t, dict)
        }
        if not self._tool_call_served and self.tool_name in tool_names:
            self._tool_call_served = True
            print("  -> serving tool_call", flush=True)
            return [
                _chunk({"reasoning_content": _PLACEHOLDER, "content": None}),
                _chunk({"content": "Let me look.\n"}),
                _chunk({
                    "tool_calls": [{
                        "index": 0, "id": "call_golden_1", "type": "function",
                        "function": {
                            "name": self.tool_name,
                            "arguments": json.dumps({"pattern": "*", "path": "."}),
                        },
                    }],
                }),
                _chunk({}, "tool_calls"),
                b"data: [DONE]\n\n",
            ]
        print("  -> serving text", flush=True)
        return [
            _chunk({"content": "Done.\n"}),
            _chunk({}, "stop"),
            b"data: [DONE]\n\n",
        ]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {"_unparsed": True}
        self.requests.append(sanitize_request(
            request.path, dict(request.headers), body,
        ))
        print(
            f"req#{len(self.requests)} {request.method} {request.path} "
            f"stream={bool(body.get('stream'))} "
            f"tools={len(body.get('tools') or [])} "
            f"continuation={_is_continuation(body)}",
            flush=True,
        )
        if request.path.endswith("/models"):
            return web.json_response({
                "object": "list",
                "data": [{"id": "capture-model", "object": "model"}],
            })
        if not body.get("stream"):
            return web.json_response({
                "id": "chatcmpl-golden", "object": "chat.completion",
                "model": "capture-model",
                "choices": [{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Done."},
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2},
            })
        frames = self._frames(body)
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
        })
        await resp.prepare(request)
        for frame in frames:
            await resp.write(frame)
        await resp.write_eof()
        # The continuation turn (assistant.tool_calls + tool result) is the
        # shape the runbook cares about. Once it arrives and we have served
        # the closing reply, stop: the client will not call again.
        if _is_continuation(body):
            self.done.set()
        return resp


def build_proxy_app(proxy: CaptureProxy) -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", proxy.handle)
    return app


async def capture(client: str, port: int, *, timeout: float,
                  tool_name: str) -> dict[str, Any]:
    """Serve until the client stops calling, then return the sanitized record.

    The proxy stops itself once the client has sent the tool-result
    continuation and we have served the closing reply, so the operator
    does not have to guess a turn count.
    """
    proxy = CaptureProxy(tool_name=tool_name)
    runner = web.AppRunner(build_proxy_app(proxy))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    print(f"listening on http://127.0.0.1:{port}", flush=True)
    print(f"point the {client} client at this base URL; Ctrl-C to stop early",
          flush=True)
    try:
        await asyncio.wait_for(proxy.done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"timeout after {timeout}s; writing what was captured", flush=True)
    finally:
        await runner.cleanup()
    return {
        "client": client,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": ("sanitized: no credentials, prompts, tool arguments, "
                 "reasoning text or absolute paths"),
        "requests": proxy.requests,
    }


def write_fixture(client: str, record: dict[str, Any]) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{client}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=False) + "\n")
    return path


def check_fixtures() -> int:
    """Fail if a committed fixture still contains private content.

    Two independent checks, because the regex scan alone cannot see the
    axis the privacy claim rests on: header *values*. A bearer token or a
    session id is not an email, a long hex run, or an absolute path, so a
    regression in ``sanitize_headers`` (dropping the sensitive branch)
    would commit a live credential and still pass a text-only scan. The
    header check re-derives the contract from ``SENSITIVE_HEADERS`` and
    ``CLIENT_HEADER_ALLOW`` rather than restating it.
    """
    problems: list[str] = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        text = path.read_text()
        for pattern, label in (
            (_EMAIL_RE, "email"), (_HEX_RE, "long hex run"),
            (_PATH_RE, "absolute path"),
        ):
            for match in pattern.findall(text):
                problems.append(f"{path.name}: {label} {match[:40]!r}")
        problems.extend(_check_fixture_headers(path))
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"{len(list(FIXTURE_DIR.glob('*.json')))} fixture(s) clean")
    return 0


def _check_fixture_headers(path: Path) -> list[str]:
    """Every recorded header value must match its sanitizer contract."""
    try:
        record = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"{path.name}: not valid JSON ({exc})"]
    problems: list[str] = []
    for index, request in enumerate(record.get("requests") or []):
        headers = request.get("headers")
        if not isinstance(headers, dict):
            problems.append(f"{path.name}: request {index} has no headers map")
            continue
        for name, value in headers.items():
            low = str(name).lower()
            if low in SENSITIVE_HEADERS:
                if value != _PLACEHOLDER:
                    problems.append(
                        f"{path.name}: request {index} leaks secret header "
                        f"{low!r} (value not {_PLACEHOLDER!r})"
                    )
            elif low not in CLIENT_HEADER_ALLOW and value != "<present>":
                problems.append(
                    f"{path.name}: request {index} records unvetted header "
                    f"{low!r} value; only presence is allowed"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", default="opencode",
                        choices=["opencode", "omp", "cline", "openai-sdk"])
    parser.add_argument("--port", type=int, default=8941)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--tool-name", default="read_file")
    parser.add_argument("--check", action="store_true",
                        help="validate committed fixtures and exit")
    parser.add_argument("--out", default="", help="override fixture path")
    args = parser.parse_args(argv)

    if args.check:
        return check_fixtures()

    record = asyncio.run(capture(
        args.client, args.port, timeout=args.timeout,
        tool_name=args.tool_name,
    ))
    if not record["requests"]:
        print("no requests captured; nothing written", file=sys.stderr)
        return 1
    path = Path(args.out) if args.out else write_fixture(args.client, record)
    print(f"wrote {path} ({len(record['requests'])} request(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
