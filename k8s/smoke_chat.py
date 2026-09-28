#!/usr/bin/env python3
"""Fail deployment unless the gateway returns a complete, useful chat stream."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import urllib.error
import urllib.request

# Executed on the build host without installing the gateway dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tusker_gateway.sse import split_sse_frame, sse_data_payload

MAX_STREAM_BYTES = 10 * 1024 * 1024


class StreamCheckError(ValueError):
    pass


def check_stream(chunks):
    buffer = b""
    total = 0
    content = []
    done = False
    try:
        for chunk in chunks:
            total += len(chunk)
            if total > MAX_STREAM_BYTES:
                raise StreamCheckError("stream exceeds byte limit")
            buffer += chunk
            while True:
                frame, remainder = split_sse_frame(buffer)
                if frame is None:
                    break
                buffer = remainder
                events = [line[6:].strip() for line in frame.splitlines() if line.startswith(b"event:")]
                if any(event in {b"error", b"response.failed", b"response.incomplete"} for event in events):
                    raise StreamCheckError("error event in stream")
                payload = sse_data_payload(frame)
                if payload is None:
                    continue
                if done:
                    raise StreamCheckError("data after terminal event")
                if payload.strip() == b"[DONE]":
                    done = True
                    continue
                try:
                    data = json.loads(payload)
                except (ValueError, UnicodeDecodeError):
                    raise StreamCheckError("invalid JSON event") from None
                if not isinstance(data, dict) or "error" in data:
                    raise StreamCheckError("error event in stream")
                if data.get("type") in {"error", "response.failed", "response.incomplete"}:
                    raise StreamCheckError("error event in stream")
                for choice in data.get("choices", []):
                    text = choice.get("delta", {}).get("content")
                    if text is not None:
                        if not isinstance(text, str):
                            raise StreamCheckError("invalid content delta")
                        content.append(text)
        if buffer.strip():
            raise StreamCheckError("incomplete trailing frame")
        if not done:
            raise StreamCheckError("stream did not terminate with [DONE]")
        if not "".join(content).strip():
            raise StreamCheckError("stream produced no nonempty assistant content")
    except (StreamCheckError, TypeError, AttributeError) as exc:
        reason = str(exc) if isinstance(exc, StreamCheckError) else "malformed stream event"
        return {"ok": False, "reason": reason, "content": ""}
    return {"ok": True, "reason": "ok", "content": "".join(content)}


def fetch_chat(url, api_key, model, timeout=120):
    if not api_key:
        return {"ok": False, "reason": "missing API key", "content": "", "http_status": None}
    request = urllib.request.Request(url, data=json.dumps({
        "model": model, "stream": True, "max_tokens": 128,
        "messages": [{"role": "user", "content": "Reply with the word DONE."}],
    }).encode(), headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        # Cloudflare edge rejects the default Python-urllib user agent
        # with error code 1010. Use a curl-like UA so the smoke helper
        # works in any environment that gates on UA.
        "User-Agent": "tusker-smoke/1.0 (curl-compatible)",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = check_stream(iter(lambda: response.read1(4096), b""))
            return {**result, "http_status": response.status}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "reason": f"HTTP {exc.code} from gateway", "content": "", "http_status": exc.code}
    except (OSError, urllib.error.URLError):
        return {"ok": False, "reason": "chat transport failure or timeout", "content": "", "http_status": None}


def _fetch_json(url, api_key, payload, timeout=45):
    if not api_key:
        return {"ok": False, "configuration_error": False, "reason": "missing API key", "http_status": None}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "tusker-smoke/1.0 (curl-compatible)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            body = json.loads(raw)
            if not isinstance(body, dict):
                return {"ok": False, "configuration_error": False, "reason": "invalid JSON response", "http_status": response.status}
            error = body.get("error")
            error_code = error.get("code") if isinstance(error, dict) else None
            if error_code in {"no_embed_providers", "no_reranker_providers"}:
                return {"ok": False, "configuration_error": True, "reason": str(error_code), "http_status": response.status}
            if error:
                return {"ok": True, "configuration_error": False, "reason": "provider returned an ordinary error", "http_status": response.status}
            return {"ok": True, "configuration_error": False, "reason": "ok", "http_status": response.status, "body": body}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except (OSError, ValueError):
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        error_code = error.get("code") if isinstance(error, dict) else None
        config_error = error_code in {"no_embed_providers", "no_reranker_providers"}
        return {
            "ok": not config_error,
            "configuration_error": config_error,
            "reason": str(error_code or f"HTTP {exc.code} from gateway"),
            "http_status": exc.code,
        }
    except (OSError, urllib.error.URLError, ValueError):
        return {"ok": False, "configuration_error": False, "reason": "media transport or JSON failure", "http_status": None}


def fetch_media(url, api_key, timeout=45):
    checks = {
        "embeddings": ("/v1/embeddings", {"model": "hermes-embed", "input": "deployment smoke"}, "data"),
        "rerank": ("/v1/rerank", {"model": "hermes-reranker", "query": "smoke", "documents": ["deployment smoke"]}, "results"),
    }
    results = {}
    for name, (path, payload, result_key) in checks.items():
        result = _fetch_json(url.rstrip("/") + path, api_key, payload, timeout)
        if result["ok"] and "body" in result and not isinstance(result["body"].get(result_key), list):
            result = {**result, "ok": False, "reason": f"response missing {result_key} list"}
        results[name] = result
    failures = [
        f"{name}: {result['reason']}"
        for name, result in results.items()
        if result["configuration_error"] or not result["ok"]
    ]
    return {"ok": not failures, "reason": "; ".join(failures) if failures else "ok", "checks": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", default="hermes-code")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--skip-media", action="store_true")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    def expired(*_):
        raise TimeoutError("smoke deadline expired")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, args.timeout)
    try:
        result = fetch_chat(args.url, os.environ.get("SMOKE_API_KEY", ""), args.model, args.timeout)
        media = None if args.skip_media else fetch_media(
            args.url.rsplit("/v1/", 1)[0], os.environ.get("SMOKE_API_KEY", ""), args.timeout
        )
        if not result["ok"]:
            combined = result
        elif media is not None and not media["ok"]:
            combined = {"ok": False, "reason": media["reason"], "http_status": None}
        else:
            combined = result
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps({k: v for k, v in combined.items() if k != "content"}))
    return 0 if combined["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
