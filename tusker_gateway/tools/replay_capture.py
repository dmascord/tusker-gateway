"""Replay a captured rejected provider turn against its original provider.

A rejected tool turn is hidden from the client by an immediate failover, so the
only way to tell whether the provider still violates the tool contract is to
re-issue the exact request the gateway sent and look at what comes back. The
turn captures written by ``tusker_gateway.turn_capture`` hold that request body
byte-for-byte, together with the verdict counters that justified the rejection.

This command reads one capture from the capture database and POSTs its stored
``request_body`` verbatim to the provider's chat endpoint, then reports the
verdict counters of the replayed turn. It is an operator diagnostic, not a
gateway path: it never talks to the gateway and it never logs the captured
payload text. Prompt-derived bytes are withheld by policy for ZDR/unknown
pools, and a truncated request excerpt cannot be replayed byte-exactly, so both
refuse instead of sending a different request to the provider.

Usage::

    python3 -m tusker_gateway.tools.replay_capture --capture-id <id>
    python3 -m tusker_gateway.tools.replay_capture --request-id <rid>
    python3 -m tusker_gateway.tools.replay_capture --list --provider openai

Exit codes:
    0   the replayed turn looks usable (the violation did not reproduce)
    2   refusal or configuration error (no request was sent)
    3   the replayed turn reproduces the unusable-tool-turn condition
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
from typing import Any, Awaitable, Callable, Iterable, Mapping

import aiohttp

from tusker_gateway.config import load_config
from tusker_gateway.sse import split_sse_frame, sse_data_payload
from tusker_gateway.tool_formats import (
    parse_text_tool_calls,
    strip_tool_text,
    tool_markup_has_opening,
    tool_markup_kinds,
)
from tusker_gateway.turn_capture import (
    BODY_CAPTURED,
    BODY_EXCLUDED_POOL,
    BODY_REQUEST_UNAVAILABLE,
    BODY_UNKNOWN_POOL,
    fetch_turn_capture,
    json_safe_capture,
    list_turn_captures,
)

# Exit codes. A refusal is never a reproduction: the operator must be able to
# distinguish "the provider still breaks" from "we could not ask the provider".
EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNUSABLE = 3

# Bounded excerpt emitted only under ``--show-body``. The captured bytes are
# prompt-derived, so nothing is printed unless the operator opts in.
_BODY_EXCERPT_CHARS = 2000

# Providers that hold their credential in the gateway runtime rather than in a
# process environment variable cannot be replayed offline.
_CREDENTIAL_POOL_AUTH = frozenset({"oauth", "codex"})

Transport = Callable[..., Awaitable[tuple[int, bytes]]]


class Refusal(Exception):
    """A replay that must not be attempted; carries a human-readable reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class ReplayError(Exception):
    """A replay that was attempted but could not be evaluated."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--capture-id", dest="capture_id", default=None)
    selection.add_argument("--list", action="store_true", dest="list_captures")
    parser.add_argument(
        "--request-id",
        dest="request_id",
        default=None,
        help="select newest match, or filter rows for --list",
    )
    parser.add_argument("--provider", default=None, help="filter for --list")
    parser.add_argument("--model", default=None, help="filter for --list")
    parser.add_argument("--pool", default=None, help="filter for --list")
    parser.add_argument("--reason", default=None, help="filter for --list")
    parser.add_argument(
        "--since",
        default=None,
        help="only captures created at/after this epoch or ISO-8601 time (--list)",
    )
    parser.add_argument("--limit", type=int, default=50, help="max rows for --list")
    parser.add_argument("--timeout", type=float, default=120.0, help="replay timeout seconds")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="re-issue the captured request N times and aggregate; for "
             "disambiguating transient upstream incidents from flaky models.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--show-body",
        action="store_true",
        dest="show_body",
        help="print a bounded excerpt of the captured request/response text",
    )
    return parser



def _parse_since(value: str | None) -> float | None:
    """Parse ``--since`` as an epoch number or an ISO-8601 timestamp."""
    if value is None:
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    moment = datetime.fromisoformat(normalized)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _iso(epoch: float | None) -> str:
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refusal_for_unreplayable(record: Mapping[str, Any]) -> Refusal | None:
    """Explain why a capture cannot be replayed byte-for-byte, if it cannot."""
    policy = str(record.get("body_policy") or "")
    truncated = int(record.get("request_truncated_bytes") or 0)
    if truncated > 0:
        # An excerpt is a *different* request; sending it would test the
        # provider against bytes the gateway never sent.
        return Refusal(
            "request_truncated",
            f"request body is a {truncated}-byte excerpt; a truncated request "
            "cannot be replayed byte-exactly",
        )
    if policy == BODY_REQUEST_UNAVAILABLE:
        return Refusal(
            "request_unavailable",
            "no request bytes were stored for this capture (body_policy=request_unavailable)",
        )
    if policy == BODY_EXCLUDED_POOL:
        return Refusal(
            "excluded_pool",
            "bodies withheld by policy: the pool is ZDR-excluded "
            "(body_policy=excluded_pool)",
        )
    if policy == BODY_UNKNOWN_POOL:
        return Refusal(
            "unknown_pool",
            "pool could not be established, so the ZDR policy is unproven and the "
            "bodies were withheld (body_policy=unknown_pool)",
        )
    if policy != BODY_CAPTURED or not record.get("replayable"):
        return Refusal(
            "not_replayable",
            f"capture is not replayable (body_policy={policy or 'unknown'})",
        )
    if record.get("request_body") is None:
        return Refusal("missing_request_body", "capture has no stored request body")
    return None


def _resolve_endpoint(
    config: Mapping[str, Any], provider: str
) -> tuple[dict[str, Any] | None, Refusal | None]:
    """Return the provider's chat endpoint, or a refusal when it is unusable."""
    registry = config.get("providers")
    endpoint_config = None
    if isinstance(registry, Mapping):
        endpoint_config = registry.get(provider.lower())
    if endpoint_config is None:
        return None, Refusal(
            "unknown_provider", f"provider {provider!r} is not in the gateway registry"
        )

    def _field(name: str) -> Any:
        if isinstance(endpoint_config, Mapping):
            return endpoint_config.get(name)
        return getattr(endpoint_config, name, None)

    base_url = str(_field("base_url") or "").strip()
    chat_path = str(_field("chat_path") or "").strip()
    auth_type = str(_field("auth_type") or _field("kind") or "bearer").strip().lower()
    auth_env = str(_field("auth_env") or "").strip() or None
    api_key_header = str(_field("api_key_header") or "").strip() or None

    if auth_type in _CREDENTIAL_POOL_AUTH:
        return None, Refusal(
            "credential_pool_auth",
            f"provider {provider!r} uses {auth_type} credentials held in the "
            "gateway's credential pool; an offline replay has no token for it",
        )
    if auth_type == "local" or not base_url.startswith(("http://", "https://")):
        return None, Refusal(
            "local_adapter",
            f"provider {provider!r} is a local process adapter; replay requires "
            "the gateway runtime",
        )
    if not chat_path:
        return None, Refusal(
            "no_chat_path", f"provider {provider!r} has no configured chat_path"
        )
    return {
        "base_url": base_url,
        "chat_path": chat_path,
        "auth_type": auth_type,
        "auth_env": auth_env,
        "api_key_header": api_key_header,
    }, None


def _resolve_token(
    config: Mapping[str, Any], provider: str, endpoint: Mapping[str, Any]
) -> str | None:
    """Resolve the provider credential from its auth env var or the key map."""
    auth_env = endpoint.get("auth_env")
    if auth_env:
        token = os.environ.get(str(auth_env), "").strip()
        if token:
            return token
    key_map = config.get("provider_api_keys")
    if isinstance(key_map, Mapping):
        for name in (provider.lower(), provider):
            candidate = key_map.get(name)
            if candidate:
                return str(candidate).strip()
    return None


def _replay_headers(endpoint: Mapping[str, Any], token: str) -> dict[str, str]:
    """Build the provider-bound headers in the configured auth style."""
    headers = {"Content-Type": "application/json"}
    if endpoint.get("auth_type") == "api_key":
        header_name = endpoint.get("api_key_header") or "api-key"
        headers[str(header_name)] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request_url(endpoint: Mapping[str, Any]) -> str:
    return f"{str(endpoint['base_url']).rstrip('/')}/{str(endpoint['chat_path']).lstrip('/')}"


async def _post_provider(
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, bytes]:
    """POST the stored request bytes to the provider. Isolated for stubbing."""
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(url, data=body, headers=dict(headers)) as response:
            return response.status, await response.read()


def _iter_events(body: bytes) -> tuple[bool, list[tuple[bytes | None, bool]]]:
    """Split a captured response into ``(data_payload, is_frame)`` pieces.

    Returns ``(streaming, pieces)``. A body that carries SSE data events is
    treated as a stream; anything else is returned whole for JSON decoding.
    """
    streaming = body.startswith(b"data:") or b"\ndata:" in body
    if not streaming:
        return False, [(body, True)]
    pieces: list[tuple[bytes | None, bool]] = []
    buffer = body
    while True:
        frame, buffer = split_sse_frame(buffer)
        if frame is None:
            break
        pieces.append((sse_data_payload(frame), True))
    return True, pieces


def analyze_response(body: bytes) -> dict[str, Any]:
    """Summarize a provider chat response without retaining its text.

    Mirrors the counters the gateway records for a rejected turn so a replay can
    be compared directly against the captured verdict: content length, native
    tool-call deltas, finish reason, and whether tool markup leaked into the
    visible content as literal text.
    """
    streaming, pieces = _iter_events(body)
    frames = 0
    non_data_frames = 0
    data_events = 0
    invalid_json_events = 0
    tool_call_deltas = 0
    content_chars = 0
    content_parts: list[str] = []
    finish_reason: str | None = None
    native_tool_calls = 0
    message_content = ""

    for payload, _is_frame in pieces:
        if not streaming:
            try:
                obj = json.loads(payload or b"")
            except (TypeError, ValueError):
                raise ReplayError(
                    "invalid_response_body",
                    "provider returned a body that is neither SSE nor valid JSON",
                )
            choices = obj.get("choices") if isinstance(obj, dict) else None
            choice = choices[0] if isinstance(choices, list) and choices else {}
            if not isinstance(choice, dict):
                choice = {}
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            message_content = message.get("content") if isinstance(message.get("content"), str) else ""
            if isinstance(message.get("tool_calls"), list):
                native_tool_calls += len(message["tool_calls"])
            reason = choice.get("finish_reason")
            if reason:
                finish_reason = str(reason)
            content_chars = len(message_content)
            continue
        frames += 1
        if payload is None:
            non_data_frames += 1
            continue
        if payload.strip() == b"[DONE]":
            data_events += 1
            continue
        try:
            obj = json.loads(payload)
        except (TypeError, ValueError):
            invalid_json_events += 1
            data_events += 1
            continue
        data_events += 1
        choices = obj.get("choices") if isinstance(obj, dict) else None
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            chunk = delta.get("content")
            if isinstance(chunk, str):
                content_parts.append(chunk)
                content_chars += len(chunk)
            if isinstance(delta.get("tool_calls"), list):
                tool_call_deltas += len(delta["tool_calls"])
            reason = choice.get("finish_reason")
            if reason:
                finish_reason = str(reason)
        usage_message = obj.get("message") if isinstance(obj, dict) else None
        if isinstance(usage_message, dict) and isinstance(usage_message.get("tool_calls"), list):
            native_tool_calls += len(usage_message["tool_calls"])

    content = "".join(content_parts) if streaming else message_content
    markup_kinds = tool_markup_kinds(content)
    parsed_text_calls = parse_text_tool_calls(content)
    visible = strip_tool_text(content)
    # The gateway counts a native streaming tool call as a tool call (it sets
    # saw_tool_call from the delta's tool_calls), so the mirror must too: a
    # provider that now answers with a real call has stopped violating.
    saw_tool_call = bool(native_tool_calls or tool_call_deltas or parsed_text_calls)
    return {
        "streaming": streaming,
        "frames": frames,
        "data_events": data_events,
        "non_data_frames": non_data_frames,
        "invalid_json_events": invalid_json_events,
        "content_chars": content_chars,
        "tool_call_deltas": tool_call_deltas,
        "native_tool_calls": native_tool_calls,
        "text_tool_calls": len(parsed_text_calls),
        "finish_reason": finish_reason,
        "saw_tool_call": saw_tool_call,
        "tool_markup_kinds": list(markup_kinds),
        "tool_markup_as_text": bool(markup_kinds),
        "tool_markup_opening": tool_markup_has_opening(content),
        "visible_content_chars": len(visible) if isinstance(visible, str) else 0,
    }


def _tools_requested(record: Mapping[str, Any]) -> bool:
    """Decide whether the captured turn was a tools turn."""
    verdict = record.get("verdict")
    if isinstance(verdict, Mapping) and "tools_requested" in verdict:
        return bool(verdict["tools_requested"])
    body = record.get("request_body")
    if not body:
        return False
    payload: Any = None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, Mapping):
        return False
    # Mirrors the gateway: a streamed turn is a tools turn when it declares
    # tools, or when tool_choice is "none" (which must produce no call).
    return bool(payload.get("stream")) and (
        bool(payload.get("tools")) or payload.get("tool_choice") == "none"
    )


def _unusable_reason(summary: Mapping[str, Any], tools_requested: bool) -> str | None:
    """Mirror the gateway's rejection rules for a tool turn.

    The gateway rejects a tools turn when an executable-looking envelope could
    not be turned into a tool call, or when the turn carried no answer at all.
    Reusing the same conditions means a non-zero result here corresponds to the
    same verdict the capture recorded.
    """
    if not tools_requested:
        return None
    if summary["tool_markup_opening"] and not summary["saw_tool_call"]:
        return "malformed_tool_response"
    if not summary["saw_tool_call"] and not summary["visible_content_chars"]:
        # The gateway only calls this an unusable tool turn when there was no
        # executable envelope at all, or the envelope never closed.
        if not summary["tool_markup_kinds"] or summary["tool_markup_opening"]:
            return "reasoning_only_or_empty"
    return None


def _capture_meta(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the non-payload capture metadata used by every output mode."""
    return {
        "capture_id": record.get("capture_id"),
        "request_id": record.get("request_id"),
        "created_at": record.get("created_at"),
        "created_at_iso": _iso(record.get("created_at")),
        "provider": record.get("provider"),
        "model": record.get("model"),
        "pool": record.get("pool"),
        "request_source": record.get("request_source"),
        "reason": record.get("reason"),
        "body_policy": record.get("body_policy"),
        "replayable": bool(record.get("replayable")),
        "request_bytes": record.get("request_bytes"),
        "request_truncated_bytes": record.get("request_truncated_bytes"),
        "response_bytes": record.get("response_bytes"),
        "response_truncated_bytes": record.get("response_truncated_bytes"),
        "verdict": record.get("verdict") or {},
    }


def _select_record(
    args: argparse.Namespace, store: Any
) -> dict[str, Any]:
    """Resolve the target capture by id or by request id (newest match)."""
    if args.capture_id:
        record = store.fetch(args.capture_id)
        if record is None:
            raise Refusal("not_found", f"no capture with id {args.capture_id!r}")
        return record
    matches = store.list_captures(request_id=args.request_id, limit=1)
    if not matches:
        raise Refusal("not_found", f"no capture for request_id {args.request_id!r}")
    record = store.fetch(matches[0]["capture_id"])
    if record is None:
        raise Refusal("not_found", f"capture {matches[0]['capture_id']!r} vanished during lookup")
    return record


def _list_rows(args: argparse.Namespace, store: Any) -> list[dict[str, Any]]:
    since = _parse_since(args.since)
    rows = store.list_captures(
        provider=args.provider,
        model=args.model,
        pool=args.pool,
        reason=args.reason,
        request_id=args.request_id,
        since=since,
        limit=args.limit,
    )
    return [_capture_meta(row) for row in rows]


_LIST_COLUMNS = (
    ("capture_id", "capture_id"),
    ("created_at", "created_at_iso"),
    ("provider/model", None),
    ("pool", "pool"),
    ("reason", "reason"),
    ("body_policy", "body_policy"),
    ("replayable", "replayable"),
    ("req_bytes", "request_bytes"),
    ("resp_bytes", "response_bytes"),
)


def _format_list_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no captures matched"
    table: list[list[str]] = []
    for row in rows:
        cells: list[str] = []
        for header, key in _LIST_COLUMNS:
            if key is None:
                cells.append(f"{row.get('provider') or '?'}/{row.get('model') or '?'}")
            elif header == "replayable":
                cells.append("yes" if row.get(key) else "no")
            else:
                value = row.get(key)
                cells.append("-" if value is None else str(value))
        table.append(cells)
    headers = [header for header, _ in _LIST_COLUMNS]
    widths = [
        max(len(headers[index]), *(len(line[index]) for line in table))
        for index in range(len(headers))
    ]
    lines = ["  ".join(headers[index].ljust(widths[index]) for index in range(len(headers)))]
    for line in table:
        lines.append("  ".join(line[index].ljust(widths[index]) for index in range(len(headers))))
    return "\n".join(lines)


def _body_excerpt(record: Mapping[str, Any], side: str) -> str | None:
    """Return a bounded utf-8 excerpt of one stored body side.

    The text view comes from ``json_safe_capture`` so the tool shares the
    module's single body-decoding convention; the base64 arm the helper also
    builds is never printed.
    """
    text = json_safe_capture({f"{side}_body": record.get(f"{side}_body")}).get(
        f"{side}_text"
    )
    if text is None:
        return None
    if len(text) <= _BODY_EXCERPT_CHARS:
        return text
    return f"{text[:_BODY_EXCERPT_CHARS]}...<{len(text) - _BODY_EXCERPT_CHARS} chars elided>"


async def _run(
    args: argparse.Namespace,
    *,
    store: Any,
    transport: Transport,
    config: Mapping[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Execute one command; return (exit code, machine-readable result)."""
    if args.list_captures:
        rows = _list_rows(args, store)
        return EXIT_OK, {"mode": "list", "captures": rows}

    record = _select_record(args, store)
    meta = _capture_meta(record)
    refusal = _refusal_for_unreplayable(record)
    if refusal is not None:
        raise refusal

    provider = str(record.get("provider") or "").strip().lower()
    if not provider or provider == "unknown":
        raise Refusal("unknown_provider", "capture does not name a provider")
    endpoint, endpoint_refusal = _resolve_endpoint(config, provider)
    if endpoint_refusal is not None:
        raise endpoint_refusal
    assert endpoint is not None
    token = _resolve_token(config, provider, endpoint)
    if not token:
        auth_env = endpoint.get("auth_env") or "PROVIDER_API_KEYS"
        raise Refusal(
            "missing_credential",
            f"no credential for provider {provider!r}; set {auth_env} to replay",
        )

    url = _request_url(endpoint)
    headers = _replay_headers(endpoint, token)
    body = bytes(record["request_body"])
    repeat_count = max(1, int(getattr(args, "repeat", 1) or 1))
    tools_requested = _tools_requested(record)
    runs: list[dict[str, Any]] = []
    last_status: int | None = None
    last_response_body: bytes = b""
    for attempt in range(repeat_count):
        status, response_body = await transport(
            url, headers=headers, body=body, timeout=args.timeout
        )
        if status < 200 or status >= 300:
            raise ReplayError(
                "upstream_http_error",
                f"provider returned HTTP {status} on attempt {attempt + 1}/{repeat_count}; "
                "replay could not be evaluated",
            )
        last_status = status
        last_response_body = response_body
        summary = analyze_response(response_body)
        reason = _unusable_reason(summary, tools_requested)
        usable = reason is None
        runs.append(
            {
                "attempt": attempt + 1,
                "http_status": status,
                "upstream_bytes": len(response_body),
                "tools_requested": tools_requested,
                "usable": usable,
                "reason": reason,
                **summary,
            }
        )
    usable_count = sum(1 for r in runs if r["usable"])
    if store is not None and repeat_count > 1 and runs:
        record_replay = getattr(store, "record_replay_run", None)
        if record_replay is not None:
            try:
                record_replay(
                    str(record.get("capture_id") or ""),
                    {
                        "repeat": repeat_count,
                        "usable_count": usable_count,
                        "provider": provider,
                        "url": url,
                        "runs": runs,
                    },
                )
            except Exception as exc:  # pragma: no cover - diagnostic best effort
                # Persistence is best effort; never let it abort the replay.
                pass
    usable = usable_count == repeat_count

    if not usable and usable_count > 0:
        aggregate_reason = "flaky"
    else:
        aggregate_reason = runs[-1]["reason"] if runs else None
    result: dict[str, Any] = {
        "mode": "replay",
        "capture": meta,
        "replay": {
            "provider": provider,
            "url": url,
            "http_status": last_status,
            "upstream_bytes": len(last_response_body),
            "tools_requested": tools_requested,
            "usable": usable,
            "reason": aggregate_reason,
            "repeat": repeat_count,
            "usable_count": usable_count,
            "runs": runs,
            **(runs[-1] if runs else {}),
        },
    }
    if args.show_body:
        result["capture"]["request_excerpt"] = _body_excerpt(record, "request")
        result["capture"]["response_excerpt"] = _body_excerpt(record, "response")
        result["replay"]["response_excerpt"] = bytes(last_response_body).decode(
            "utf-8", errors="replace"
        )[:_BODY_EXCERPT_CHARS]
    return (EXIT_OK if usable else EXIT_UNUSABLE), result


def _print_result(result: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, sort_keys=True, default=str))
        return
    if result.get("mode") == "list":
        print(_format_list_table(list(result.get("captures") or [])))
        return
    meta = result.get("capture") or {}
    print(
        "capture_id={capture_id} request_id={request_id} created_at={created_at_iso} "
        "provider={provider} model={model} pool={pool} reason={reason} "
        "body_policy={body_policy} replayable={replayable} "
        "request_bytes={request_bytes} response_bytes={response_bytes}".format(**meta)
    )
    replay = result.get("replay") or {}

    repeat = int(replay.get("repeat") or 1)
    usable_count = int(replay.get("usable_count") or (1 if replay.get("usable") else 0))
    print(
        "replay provider={provider} url={url} http_status={http_status} "
        "upstream_bytes={upstream_bytes} frames={frames} data_events={data_events} "
        "content_chars={content_chars} tool_call_deltas={tool_call_deltas} "
        "finish_reason={finish_reason} tools_requested={tools_requested}".format(**replay)
    )
    if repeat > 1:
        print(
            "repeat={repeat} usable_count={usable_count}/{repeat} aggregate_reason={aggregate_reason}".format(
                repeat=repeat,
                usable_count=usable_count,
                aggregate_reason=replay.get("reason") or "none",
            )
        )
    print(
        "tool_markup_as_text={tool_markup_as_text} markup_kinds={markup} "
        "usable={usable} reason={reason}".format(
            tool_markup_as_text=replay.get("tool_markup_as_text"),
            markup=",".join(replay.get("tool_markup_kinds") or []) or "none",
            usable=replay.get("usable"),
            reason=replay.get("reason") or "none",
        )
    )

    if "request_excerpt" in meta:
        print("--- captured request excerpt (prompt-derived) ---")
        print(meta["request_excerpt"])
    if "response_excerpt" in meta:
        print("--- captured response excerpt (prompt-derived) ---")
        print(meta["response_excerpt"])
    if "response_excerpt" in replay:
        print("--- replayed response excerpt (prompt-derived) ---")
        print(replay["response_excerpt"])


def main(
    argv: Iterable[str] | None = None,
    *,
    store: Any = None,
    transport: Transport | None = None,
    config: Mapping[str, Any] | None = None,
) -> int:
    """Run the replay command. ``store``/``transport``/``config`` are test seams."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    if args.capture_id is not None and args.request_id is not None:
        print("--capture-id and --request-id cannot be combined", file=sys.stderr)
        return EXIT_REFUSED
    if args.capture_id is None and args.request_id is None and not args.list_captures:
        print(
            "one of --capture-id, --request-id, or --list is required",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    try:
        selected_store = store if store is not None else _load_store()
        selected_config = config if config is not None else load_config()
    except Exception as exc:  # pragma: no cover - environment failure path
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    def _run_async(coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        # Test harnesses may call the synchronous CLI while their own event
        # loop is active. Run the isolated CLI coroutine in a short-lived
        # worker loop rather than attempting nested run_until_complete().
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()

    runner = _run(
        args,
        store=selected_store,
        transport=transport or _post_provider,
        config=selected_config,
    )
    try:
        exit_code, result = _run_async(runner)
    except Refusal as refusal:
        print(f"refused ({refusal.reason}): {refusal.message}", file=sys.stderr)
        if args.as_json:
            print(json.dumps({"mode": "refusal", "reason": refusal.reason,
                              "message": refusal.message}, sort_keys=True))
        return EXIT_REFUSED
    except ReplayError as error:
        print(f"replay failed ({error.reason}): {error.message}", file=sys.stderr)
        if args.as_json:
            print(json.dumps({"mode": "error", "reason": error.reason,
                              "message": error.message}, sort_keys=True))
        return EXIT_REFUSED
    except Exception as exc:
        print(
            f"replay failed (transport_error): {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        if args.as_json:
            print(json.dumps({"mode": "error", "reason": "transport_error",
                              "message": str(exc)}, sort_keys=True))
        return EXIT_REFUSED

    _print_result(result, args.as_json)
    return exit_code


class _EnvStore:
    """Reader backed by the module-level capture helpers.

    The gateway process resolves its capture policy from the environment, so
    the CLI does the same: it delegates to ``fetch_turn_capture`` /
    ``list_turn_captures`` rather than opening the database itself.
    """

    def fetch(self, capture_id: str) -> dict[str, Any] | None:
        return fetch_turn_capture(capture_id)

    def list_captures(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list_turn_captures(**kwargs)


def _load_store() -> Any:
    """Open the capture store from the process environment (gateway policy)."""
    from tusker_gateway.turn_capture import capture_store

    if capture_store() is None:
        raise Refusal(
            "capture_disabled",
            "turn capture is disabled; set TUSKER_STREAM_CAPTURE to read captures",
        )
    return _EnvStore()


if __name__ == "__main__":
    sys.exit(main())