"""OTLP/HTTP-JSON compatible trace exporter.

This module implements just enough of the OpenTelemetry trace data model and
OTLP/HTTP-JSON wire format to export spans from tusker-gateway to a collector
(OpenTelemetry Collector, Jaeger, Tempo, Honeycomb, etc.) without taking on
the full OpenTelemetry SDK as a dependency.

Wire format:
    POST {endpoint}/v1/traces
    Content-Type: application/json
    Body: {"resourceSpans": [{"resource": {...}, "scopeSpans": [...]}]}

Each span we emit has:
    name           (e.g. "chat_completion", "provider_call")
    trace_id       (16-byte hex string)
    span_id        (8-byte hex string)
    parent_span_id (optional, 8-byte hex string)
    start_time_ns / end_time_ns (Unix nanoseconds)
    attributes     (flat string-keyed dict; non-string values stringified)
    status         ("ok" | "error"; error includes a message)

Span model:
    A request enters `chat_completions_handler` -> we open a top-level span.
    Sub-operations (cache lookup, budget check, provider call) become child
    spans via the `child_span()` context manager.

Transport:
    Background task batches spans (max 100 per batch or 5s flush interval)
    and POSTs them to the OTLP endpoint. Failures are logged and dropped —
    tracing must never break the gateway.

Opt-in:
    `TUSKER_OTLP_ENDPOINT` set = enabled.
    `TUSKER_OTLP_HEADERS` (optional JSON dict of headers to add, e.g. auth).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


logger = logging.getLogger(__name__)


def _ns_now() -> int:
    return time.time_ns()


def _new_trace_id() -> str:
    return secrets.token_hex(16)


def _new_span_id() -> str:
    return secrets.token_hex(8)


def _coerce(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    start_time_ns: int = 0
    end_time_ns: int = 0
    attributes: dict[str, str] = field(default_factory=dict)
    status: str = "ok"  # "ok" | "error"
    status_message: str = ""

    def to_otlp(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": "SPAN_KIND_INTERNAL",
            "startTimeUnixNano": str(self.start_time_ns),
            "endTimeUnixNano": str(self.end_time_ns),
            "attributes": [
                {"key": k, "value": {"stringValue": v}}
                for k, v in self.attributes.items()
            ],
            "status": {"code": 1 if self.status == "ok" else 2},
        }
        if self.parent_span_id:
            out["parentSpanId"] = self.parent_span_id
        if self.status == "error" and self.status_message:
            out["status"]["message"] = self.status_message
        return out


@dataclass
class TracerConfig:
    endpoint: str = ""  # e.g. "http://otel-collector:4318"
    service_name: str = "tusker-gateway"
    headers: dict[str, str] = field(default_factory=dict)
    flush_interval_secs: float = 5.0
    batch_size: int = 100
    # If True, export also writes to stdout — useful for debugging.
    stdout_export: bool = False


class Tracer:
    """Minimal OpenTelemetry-compatible tracer."""

    def __init__(self, config: TracerConfig):
        self._config = config
        self._enabled = bool(config.endpoint)
        self._buffer: list[Span] = []
        self._lock = asyncio.Lock() if self._enabled else None  # type: ignore
        self._flush_task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        """Start the background flush task. Call once at app startup."""
        if not self._enabled:
            return
        loop = asyncio.get_running_loop()
        self._flush_task = loop.create_task(self._flush_loop())

    async def stop(self) -> None:
        if not self._enabled:
            return
        self._stopping = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
        # Final flush
        await self._flush()

    # -- span creation --------------------------------------------------

    @contextmanager
    def span(self, name: str, *, attributes: dict[str, Any] | None = None,
             parent: "Span | None" = None) -> Iterator[Span]:
        """Synchronous span context (suitable for tight code paths).

        Use `parent=` to nest under a specific span; otherwise the most
        recently created span (tracked via `_current_parent`) is used.
        """
        sp = Span(
            name=name,
            trace_id=parent.trace_id if parent else _new_trace_id(),
            span_id=_new_span_id(),
            parent_span_id=parent.span_id if parent else _last_span_id(),
            start_time_ns=_ns_now(),
        )
        for k, v in (attributes or {}).items():
            sp.attributes[k] = _coerce(v)
        _push_current(sp)
        try:
            yield sp
        except Exception as exc:
            sp.status = "error"
            sp.status_message = str(exc)[:200]
            sp.attributes["exception.type"] = type(exc).__name__
            sp.attributes["exception.message"] = str(exc)[:500]
            sp.attributes["exception.stacktrace"] = traceback.format_exc()[:2000]
            raise
        finally:
            sp.end_time_ns = _ns_now()
            self._enqueue(sp)
            _pop_current(sp)

    # -- transport -------------------------------------------------------

    def _enqueue(self, span: Span) -> None:
        if not self._enabled:
            return
        # We append without the lock; the flush task takes the lock.
        self._buffer.append(span)
        if len(self._buffer) >= self._config.batch_size:
            # Schedule a flush; don't block the caller.
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._flush())
            except RuntimeError:
                # No running loop — flush synchronously is unsafe; drop.
                self._buffer.clear()

    async def _flush_loop(self) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(self._config.flush_interval_secs)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        if not self._buffer:
            return
        if self._lock is None:
            return
        async with self._lock:
            batch = self._buffer[: self._config.batch_size]
            self._buffer = self._buffer[self._config.batch_size :]
        if not batch:
            return
        await self._export(batch)

    async def _export(self, batch: list[Span]) -> None:
        url = self._config.endpoint.rstrip("/") + "/v1/traces"
        otlp_spans = [s.to_otlp() for s in batch]
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": self._config.service_name}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "tusker-gateway", "version": "0.1.0"},
                            "spans": otlp_spans,
                        }
                    ],
                }
            ]
        }
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(
                    url, json=body, headers={"Content-Type": "application/json", **self._config.headers}
                ) as resp:
                    if resp.status >= 400:
                        logger.warning("OTLP export failed: %s %s", resp.status, await resp.text()[:500])
        except Exception as exc:
            logger.warning("OTLP export error: %s", exc)
        if self._config.stdout_export:
            print("[OTLP]", json.dumps(body, indent=2)[:2000])


# -- Current-span tracking (process-local) --------------------------------

_current: list[Span] = []


def _push_current(span: Span) -> None:
    _current.append(span)


def _pop_current(span: Span) -> None:
    if _current and _current[-1] is span:
        _current.pop()
    else:
        try:
            _current.remove(span)
        except ValueError:
            pass


def _last_span_id() -> str | None:
    return _current[-1].span_id if _current else None


def load_tracer_config_from_env(env: dict[str, str] | None = None) -> TracerConfig:
    env = env if env is not None else os.environ
    endpoint = env.get("TUSKER_OTLP_ENDPOINT", "").strip()
    headers_raw = env.get("TUSKER_OTLP_HEADERS", "").strip()
    headers: dict[str, str] = {}
    if headers_raw:
        try:
            headers = json.loads(headers_raw)
        except json.JSONDecodeError:
            pass
    return TracerConfig(
        endpoint=endpoint,
        service_name=env.get("TUSKER_OTLP_SERVICE_NAME", "tusker-gateway"),
        headers=headers,
        flush_interval_secs=float(env.get("TUSKER_OTLP_FLUSH_SECS", "5")),
        batch_size=int(env.get("TUSKER_OTLP_BATCH", "100")),
        stdout_export=env.get("TUSKER_OTLP_STDOUT", "false").lower() in ("1", "true", "yes"),
    )


__all__ = [
    "Span",
    "Tracer",
    "TracerConfig",
    "load_tracer_config_from_env",
]
