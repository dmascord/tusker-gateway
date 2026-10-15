"""Bounded, content-free integrity diagnostics for outgoing Chat Completions SSE."""
from __future__ import annotations

import json
from typing import Any

from tusker_gateway.tool_telemetry import summarize_chat_tool_result


class StreamToolTelemetry:
    """Track streamed calls without retaining tool arguments or reasoning text."""

    def __init__(self, *, requested: bool):
        self.requested = requested
        self._pending = b""
        self._calls: dict[tuple[int, int], dict[str, Any]] = {}
        self._finish = ""
        self._reasoning = False
        self._done = False
        self._invalid_frames = 0
        self._conflicting_ids = 0
        self._oversize = 0

    def reset_for_fallback(self) -> None:
        """Discard abandoned candidate data before observing a replacement."""
        self._pending = b""
        self._calls.clear()
        self._finish = ""
        self._reasoning = False
        self._done = False

    def feed(self, chunk: bytes) -> None:
        self._pending += chunk
        if len(self._pending) > 131072:
            self._pending = b""
            self._oversize += 1
            return
        while b"\n\n" in self._pending:
            frame, self._pending = self._pending.split(b"\n\n", 1)
            for line in frame.splitlines():
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    self._done = True
                    continue
                try:
                    event = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    self._invalid_frames += 1
                    continue
                if not isinstance(event, dict):
                    continue
                choices = event.get("choices")
                if not isinstance(choices, list):
                    continue
                for choice in choices[:16]:
                    if not isinstance(choice, dict):
                        continue
                    finish = choice.get("finish_reason")
                    if isinstance(finish, str):
                        self._finish = finish[:48]
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        continue
                    self._reasoning |= bool(delta.get("reasoning_content"))
                    for raw in (delta.get("tool_calls") or [])[:128]:
                        if not isinstance(raw, dict):
                            continue
                        index = raw.get("index", 0)
                        choice_index = choice.get("index", 0)
                        if not isinstance(index, int) or not isinstance(choice_index, int):
                            continue
                        key = (choice_index, index)
                        if key not in self._calls:
                            if len(self._calls) >= 128:
                                self._oversize += 1
                                continue
                            self._calls[key] = {"id": "", "arguments": "", "has_function": False}
                        item = self._calls[key]
                        call_id = raw.get("id")
                        if isinstance(call_id, str) and call_id:
                            if item["id"] and item["id"] != call_id:
                                self._conflicting_ids += 1
                            item["id"] = call_id
                        function = raw.get("function")
                        if isinstance(function, dict):
                            item["has_function"] = True
                            arguments = function.get("arguments")
                            if isinstance(arguments, str):
                                if len(item["arguments"]) + len(arguments) <= 65536:
                                    item["arguments"] += arguments
                                else:
                                    self._oversize += 1

    def emitted_call_ids(self) -> list[str]:
        """Return transient call IDs for correlation; never log them."""
        return [item["id"] for item in self._calls.values() if item["id"]]

    def summary(self) -> dict[str, Any]:
        calls = [
            {"id": item["id"], "function": {"arguments": item["arguments"]}}
            if item["has_function"] else {"id": item["id"]}
            for item in self._calls.values()
        ]
        result = {"choices": [{"finish_reason": self._finish,
                              "message": {"reasoning_content": "present" if self._reasoning else "",
                                          "tool_calls": calls}}]}
        summary = summarize_chat_tool_result(result, requested=self.requested)
        summary.update({
            "protocol": "openai_sse",
            "conflicting_ids": self._conflicting_ids,
            "invalid_frames": self._invalid_frames,
            "oversize_frames": self._oversize,
            "incomplete_stream": not self._done or bool(self._pending),
            "finish_mismatch": bool(calls) and self._finish not in ("tool_calls", ""),
        })
        return summary
