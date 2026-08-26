"""Endpoint handlers: /models, /chat/completions, /responses."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from typing import Any, AsyncIterator

from aiohttp import web

from tusker_gateway.cache import ResponseCache, make_cache_key
from tusker_gateway.budget import BudgetTracker
from tusker_gateway.circuit_breaker import CircuitBreaker, BreakerDecision
from tusker_gateway.errors import (
    BadRequestError,
    GatewayError,
    RateLimitError,
    openai_error,
)
from tusker_gateway.metrics import MetricsRegistry
from tusker_gateway.passthrough import PassthroughClient
from tusker_gateway.pools import PoolManager
from tusker_gateway.quality import QualityDB
from tusker_gateway.rate_limit import RateLimiter
from tusker_gateway.routing import resolve_route
from tusker_gateway.sse import (
    format_openai_chunk,
    sse_done,
    sse_frame,
    sse_heartbeat_loop,
)
from tusker_gateway.tracing import Tracer

logger = logging.getLogger(__name__)


# Heartbeat interval for client-facing SSE streams. Must be comfortably below
# the idle-connection timeouts of common intermediaries:
#   - Traefik default `Transport.RespondingTimeouts.IdleTimeout` = 60s
#   - nginx `proxy_read_timeout` (commonly)               = 60s
#   - Cloudflare free tier                                 = 100s
#   - CloudFront                                          = 5m (safe)
# 15s gives us at least 4 heartbeats before any of these fire.
#
# Read at request time (not import time) so tests can monkeypatch the
# env var without re-importing the module, and operators can flip the
# knob in production via the deployment manifest without a redeploy.
def _sse_heartbeat_secs() -> float:
    return float(os.environ.get("TUSKER_SSE_HEARTBEAT_SECS", "15"))


_TOOL_CALL_XML_RE = re.compile(
    r"<\|?\s*/?\s*tool_call\s*\|?>|<function=[\s\S]*?</function>",
    re.IGNORECASE,
)

# Matches the opening of a bare <function=name> block. Used by the stripper to
# detect when a block has begun but is not yet complete (and may span chunks).
_FUNCTION_OPEN_RE = re.compile(r"<function=[^\s\"'<>]+>", re.IGNORECASE)
_FUNCTION_CLOSE_RE = re.compile(r"</\s*function\s*>", re.IGNORECASE)
# Matches a <parameter=...>...</parameter> sibling inside a function block.
# Used to decide whether a buffered <function=...>...</function> block is
# actually a tool call (with real arguments) versus a model that opened a
# block, wrote prose inside it, and closed it without ever emitting params.
_PARAMETER_RE = re.compile(r"<\s*parameter\b", re.IGNORECASE)
# Orphan closing tags — closing tags that arrive with no matching opener
# observed by the stripper. Models occasionally emit these when they think
# they're inside a tool-call block (e.g., they've been instructed to use
# tools via prompt) but never wrote a real <function=...> opener. Without
# stripping them, OMP renders `</parameter></function>` as visible text.
_ORPHAN_CLOSE_RE = re.compile(
    r"</\s*(?:function|parameter|invoke|tool_call|function_call|tool_calls|function_calls|tool_use|tool_invocation)\s*>",
    re.IGNORECASE,
)
# Detect a tail that *might* continue into a `<function=...>` opener but
# isn't an exact prefix of any known opener. Used to carry partial tokens
# like `<func`, `<function`, `<function=`, `< function`, `< function =` etc.
#
# This is conservative: it only fires when the tail unambiguously looks
# like the start of a tool-call markup (i.e. begins with `<` immediately
# followed by an identifier or whitespace). Stray `<` characters in
# ordinary prose are NOT carried because that would silently swallow
# legitimate text from the user's view.
#
# Note: split-opener edge cases (where a model emits `<functi` in one
# chunk and `on=bash>` in the next) are not handled. In practice the
# upstream stream boundary aligns with the token boundary for
# well-behaved providers, so the full `<function=bash>` opener usually
# arrives in a single chunk. When it doesn't, the markup leaks as
# visible text — a known limitation documented in
# `docs/zero-downtime-deploys.md`-style notes.
_PARTIAL_OPENER_TAIL_RE = re.compile(r"^<\s*[a-zA-Z_:|\-]*$")

# Opening tokens that mark the start of a tool-call block. When the tail of
# the accumulated buffer matches one of these prefixes, we hold it back until
# the full block has arrived (a block may span several streamed chunks).
_TOOL_OPENERS = (
    "<​tool_call>",
    "<tool_call",
    "<|tool_call|>",
    "<|tool_call",
    "<|start_header|>",
    "<function=",
    "<|begin_of_",
)


def _extract_inner_prose(block: str) -> str:
    """Extract the prose between a bare ``<function=name>`` opener and its
    ``</function>`` closer, stripping any orphan closing tags inside.

    Used by the stream normalizer to surface ordinary text the model
    accidentally wrapped in a malformed tool-call block.
    """
    open_match = _FUNCTION_OPEN_RE.search(block)
    if not open_match:
        return ""
    close_match = _FUNCTION_CLOSE_RE.search(block, open_match.end())
    if not close_match:
        return ""
    inner = block[open_match.end():close_match.start()]
    # Strip orphan closing tags inside the inner prose.
    inner = _ORPHAN_CLOSE_RE.sub("", inner)
    return inner.strip()


def _content_frame(content: str, template_obj: dict[str, Any]) -> bytes:
    """Build an SSE frame containing only ``delta.content`` (no finish_reason).

    Used to emit prose extracted from a malformed function block.
    """
    chunk = {
        "id": template_obj.get("id") or f"chatcmpl-{secrets.token_hex(14)}",
        "object": "chat.completion.chunk",
        "model": template_obj.get("model") or "tusker-gateway",
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


class _ToolCallStripper:
    """Stateful stripper that removes XML/Markdown tool_call markup from a
    streamed text sequence, tolerating block boundaries that split across
    network chunks.

    Holds a ``_carry`` tail that may be an incomplete opening token; when a
    complete block is seen it is dropped, otherwise carried text is emitted.

    For the bare ``<function=name>...</function>`` shape (no surrounding
    ``<​tool_call>`` wrapper) — emitted by Hermes/Qwen-style models — the
    stripper additionally tracks any *complete* such block it observed in the
    stream so the caller can promote the markup into a structured
    ``delta.tool_calls`` frame. Use ``drain_pending_blocks()`` to retrieve
    them.

    The stripper distinguishes *well-formed* function blocks (those that
    contain at least one ``<parameter=k>v</parameter>`` sibling) from
    *malformed* ones (model wrote prose inside the block, or emitted only
    closing tags). Only well-formed blocks are eligible for promotion to a
    structured ``delta.tool_calls``; malformed blocks have their markup
    stripped but the inner prose is emitted as ordinary text.
    """

    def __init__(self) -> None:
        self._carry = ""
        # When an unclosed bare <function=...> block is observed we buffer it
        # here until </function> arrives (or flush() drops it). Buffering
        # avoids the case where the opener regex finds a partial match but
        # the closing tag arrives in a separate chunk.
        self._pending_function_block: str | None = None
        # Blocks that completed (or matched a single-chunk regex) get
        # stashed here so the caller can promote them into structured
        # ``delta.tool_calls`` frames. We track them as tuples of
        # ``(block, had_params)`` so the caller can decide whether each
        # block is well-formed.
        self._pending_blocks: list[tuple[str, bool]] = []

    def _looks_like_opener(self, text: str) -> bool:
        """Return True if `text` is a prefix of a known tool-call opener."""
        if not text:
            return False
        lower = text.lower()
        for opener in _TOOL_OPENERS:
            if opener.startswith(lower):
                return True
        return False

    def _looks_like_partial_opener_tail(self, text: str) -> bool:
        """Return True if `text` could be the prefix of a tool-call opener
        but doesn't yet match a known opener (e.g. ``<func``, ``<function``,
        ``< function``). Used to carry partial tokens across chunks so that
        split openers can be reassembled.
        """
        if not text:
            return False
        # Anything that's already a known opener is handled elsewhere.
        if self._looks_like_opener(text):
            return True
        # Otherwise look for a tail that's clearly the start of *some* tool
        # markup. ``_PARTIAL_OPENER_TAIL_RE`` matches `<` followed by an
        # in-progress identifier (letters, `:`, `|`, `-`).
        return bool(_PARTIAL_OPENER_TAIL_RE.search(text))

    def _stash_block(self, block: str, had_params: bool) -> None:
        self._pending_blocks.append((block, had_params))

    def feed(self, chunk: str) -> str:
        """Process a content chunk, returning the clean (emit-able) text."""
        if not chunk:
            return ""
        text = self._carry + chunk
        self._carry = ""
        out: list[str] = []

        # If we're mid-block (previously saw an unclosed <function=...>),
        # buffer the new text and look for the matching </function>. Only
        # when we find the closer do we add the block to _pending_blocks
        # and resume normal text emission.
        if self._pending_function_block is not None:
            closer = _FUNCTION_CLOSE_RE.search(text)
            if not closer:
                # Buffer the chunk. We re-check for parameters on the full
                # assembled block when the closer arrives (in case the
                # <parameter ...> tag was split across chunks).
                self._pending_function_block += text
                return ""
            # Block closes within this chunk. Stash the complete block and
            # process the remaining text below. Check the *full* assembled
            # block for parameter siblings — handles the case where a
            # parameter tag was split mid-token across chunks.
            tail = text[closer.end():]
            self._pending_function_block += text[: closer.end()]
            had_params = bool(_PARAMETER_RE.search(self._pending_function_block))
            self._stash_block(self._pending_function_block, had_params)
            self._pending_function_block = None
            text = tail

        # Repeatedly remove complete tool-call blocks. For bare <function=...>
        # blocks we additionally retain a copy of the matched block in
        # ``_pending_blocks`` so the caller can promote it into structured
        # tool_calls instead of just dropping it.
        while True:
            m = _TOOL_CALL_XML_RE.search(text)
            if not m:
                break
            out.append(text[: m.start()])
            block = m.group(0)
            if _FUNCTION_OPEN_RE.match(block):
                # Track whether this single-chunk block has real parameters.
                had_params = bool(_PARAMETER_RE.search(block))
                self._stash_block(block, had_params)
            text = text[m.end():]

        # Strip orphan closing tags (`</parameter>`, `</function>`, etc.) that
        # arrive with no matching opener observed by the stripper. Models
        # occasionally emit these when they think they're already inside a
        # tool-call block (e.g., instructed via prompt to use tools) but never
        # wrote a real `<function=...> opener. Without stripping them OMP
        # renders `</parameter></function>` as visible text content.
        text = _ORPHAN_CLOSE_RE.sub("", text)

        # Detect an *unclosed* bare <function=...> opener in the remaining
        # text. If found, split: emit prose up to the opener, buffer the
        # rest as the start of a cross-chunk block.
        open_match = _FUNCTION_OPEN_RE.search(text)
        if open_match:
            out.append(text[: open_match.start()])
            self._pending_function_block = text[open_match.start():]
            # Parameter detection is done on the assembled block at close
            # time (see the close-detection code above), so we don't track
            # it per-chunk here.
            return "".join(out)

        # No unclosed opener. If `text` ends with an incomplete opener
        # prefix that may continue into the next chunk, carry that prefix
        # and emit the rest. We walk the last ``max_opener_len`` characters
        # only — a stray ``<`` in the middle of ordinary text (e.g. ``1 < 2``)
        # outside that window is not considered.
        if text:
            carry = ""
            max_opener_len = 64
            search_from = max(0, len(text) - max_opener_len)
            for i in range(search_from, len(text)):
                tail = text[i:]
                if self._looks_like_opener(tail) or self._looks_like_partial_opener_tail(tail):
                    carry = tail
                    break
            if carry:
                out.append(text[: -len(carry)])
            else:
                out.append(text)
            self._carry = carry
        return "".join(out)

    def flush(self) -> str:
        """Drop any remaining carried content (partial opener that never
        completed). Returns the clean emitted text, usually empty."""
        carry, self._carry = self._carry, ""
        # Drop any unclosed function block — we never received its
        # </function>, so it's not safe to promote.
        self._pending_function_block = None
        return ""

    def drain_pending_blocks(self) -> list[tuple[str, bool]]:
        """Return and clear any fully-observed ``<function=...>...</function>``
        blocks seen during ``feed()`` calls since the last drain. Each entry
        is ``(block_text, had_parameters)`` where ``had_parameters`` is True
        iff the block contained at least one ``<parameter=k>v</parameter>``
        sibling.

        Used by the stream normalizer to promote text-emitted tool calls
        into structured ``delta.tool_calls`` frames. Only blocks with
        ``had_parameters=True`` should be promoted; malformed blocks (no
        parameters) should be dropped and their inner prose emitted as
        ordinary text.
        """
        blocks, self._pending_blocks = self._pending_blocks, []
        return blocks
        text = self._carry + chunk
        self._carry = ""
        out: list[str] = []

        # If we're mid-block (previously saw an unclosed <function=...>),
        # buffer the new text and look for the matching </function>. Only
        # when we find the closer do we add the block to _pending_blocks
        # and resume normal text emission.
        if self._pending_function_block is not None:
            closer = _FUNCTION_CLOSE_RE.search(text)
            if not closer:
                # Buffer the chunk. We re-check for parameters on the full
                # assembled block when the closer arrives (in case the
                # <parameter ...> tag was split across chunks).
                self._pending_function_block += text
                return ""
            # Block closes within this chunk. Stash the complete block and
            # process the remaining text below. Check the *full* assembled
            # block for parameter siblings — handles the case where a
            # parameter tag was split mid-token across chunks.
            tail = text[closer.end():]
            self._pending_function_block += text[: closer.end()]
            had_params = bool(_PARAMETER_RE.search(self._pending_function_block))
            self._pending_blocks.append((self._pending_function_block, had_params))
            self._pending_function_block = None
            text = tail

        # Repeatedly remove complete tool-call blocks. For bare <function=...>
        # blocks we additionally retain a copy of the matched block in
        # ``_pending_blocks`` so the caller can promote it into structured
        # tool_calls instead of just dropping it.
        while True:
            m = _TOOL_CALL_XML_RE.search(text)
            if not m:
                break
            out.append(text[: m.start()])
            block = m.group(0)
            if _FUNCTION_OPEN_RE.match(block):
                # Track whether the block has real <parameter=k>...</parameter>
                # siblings so we can decide later whether to promote it.
                had_params = bool(_PARAMETER_RE.search(block))
                self._pending_blocks.append((block, had_params))
            text = text[m.end():]

        # Strip orphan closing tags (`</parameter>`, `</function>`, etc.) that
        # arrive with no matching opener observed by the stripper. Models
        # occasionally emit these when they think they're already inside a
        # tool-call block (e.g., instructed via prompt to use tools) but never
        # wrote a real `<function=...>` opener. Without stripping them OMP
        # renders `</parameter></function>` as visible text content.
        text = _ORPHAN_CLOSE_RE.sub("", text)

        # Detect an *unclosed* bare <function=...> opener in the remaining
        # text. If found, split: emit prose up to the opener, buffer the
        # rest as the start of a cross-chunk block.
        open_match = _FUNCTION_OPEN_RE.search(text)
        if open_match:
            out.append(text[: open_match.start()])
            self._pending_function_block = text[open_match.start():]
            return "".join(out)

        # No unclosed opener. If `text` ends with an incomplete opener
        # prefix that may continue into the next chunk, carry it;
        # otherwise emit everything.
        if text:
            emit_end = len(text)
            carry = ""
            for i in range(len(text)):
                tail = text[i:]
                if self._looks_like_opener(tail):
                    emit_end = i
                    carry = tail
                    break
            out.append(text[:emit_end])
            self._carry = carry
        return "".join(out)

    def flush(self) -> str:
        """Drop any remaining carried content (partial opener that never
        completed). Returns the clean emitted text, usually empty."""
        carry, self._carry = self._carry, ""
        return ""

    def drain_pending_blocks(self) -> list[tuple[str, bool]]:
        """Return and clear any fully-observed ``<function=...>...</function>``
        blocks seen during ``feed()`` calls since the last drain. Each entry
        is ``(block_text, had_parameters)`` where ``had_parameters`` is True
        iff the block contained at least one ``<parameter=k>v</parameter>``
        sibling.

        Used by the stream normalizer to promote text-emitted tool calls
        into structured ``delta.tool_calls`` frames. Only blocks with
        ``had_parameters=True`` should be promoted; malformed blocks (no
        parameters) should be dropped and their inner prose emitted as
        ordinary text.
        """
        blocks, self._pending_blocks = self._pending_blocks, []
        return blocks


def _strip_xml_tool_calls(content: str) -> str:
    """Strip XML/Markdown-style tool_call markup from assistant content.

    Some open-source models (e.g. DeepSeek, Qwen) emit tool-call markup
    as raw text in the content stream even when tools are provided via
    the structured `tool_calls` API field. This causes clients like
    OMP to see duplicate tool-call artifacts ("text tool calls leaking
    through"). We strip both the wrapper tags and the inner function
    payloads so the content stream only carries the prose.

    We only strip when the markup contains tool-call-shaped tags so normal
    text mentioning "tool_call" is preserved.
    """
    if not content or "<" not in content:
        return content
    return _TOOL_CALL_XML_RE.sub("", content)


async def _normalize_stream(raw_stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Normalize upstream SSE chunks for OMP/client compatibility.

    Some upstream providers bundle `delta.content` and `finish_reason` in the
    same SSE chunk. OMP (and other strict OpenAI clients) require:
      - content-only delta chunks with finish_reason: null
      - a separate final chunk with finish_reason set
    This generator splits bundled chunks into separate SSE frames.

    Additionally, some open-source models emit XML/Markdown-style
    ``<​tool_call>...<function=...>...</function></​tool_call>`` markup in
    the content stream alongside structured ``tool_calls`` deltas. This
    shows up in OMP as raw text tool calls "leaking through". We strip
    that markup from content deltas so OMP sees only the structured
    tool_calls and the surrounding prose.

    For models that emit bare ``<function=name>...</function>`` blocks
    (Hermes/Qwen-style, often without an enclosing ``<​tool_call>``
    wrapper) we additionally promote the recognized block into a
    structured ``delta.tool_calls`` frame so OMP receives a real
    ``tool_calls`` array and not just stripped text. Blocks may span
    multiple SSE deltas; the stripper buffers them across chunks.

    The upstream yields arbitrary byte chunks from `resp.content.iter_any()`,
    which may contain multiple SSE events. We split on ``\\n\\n`` boundaries,
    process each ``data:`` event individually, and re-emit them.
    """
    from tusker_gateway.tool_formats import parse_text_tool_calls

    buffer = b""
    tool_stripper = _ToolCallStripper()
    saw_finish_reason = False
    saw_done = False

    def _finish_frame() -> bytes:
        return sse_frame(format_openai_chunk(finish_reason="stop"))

    def _tool_calls_frame(parsed_calls: list[dict[str, Any]], template_obj: dict[str, Any]) -> bytes | None:
        """Build an SSE frame carrying the parsed tool calls as delta.tool_calls.

        Returns ``None`` if there are no calls to emit.
        """
        if not parsed_calls:
            return None
        # OpenAI stream spec: each tool_calls delta carries an incremental
        # index, an id, type, and function name/arguments. Our rescue
        # arrived as a single assembled block, so we emit it whole — clients
        # (including OMP) accept a single-frame tool_call just fine.
        openai_calls: list[dict[str, Any]] = []
        for index, call in enumerate(parsed_calls):
            fn = call.get("function") or {}
            name = fn.get("name", "")
            args = fn.get("arguments", "{}")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            cid = call.get("id") or f"call_{index}_{hashlib.sha1(name.encode()).hexdigest()[:10]}"
            openai_calls.append({
                "index": index,
                "id": str(cid),
                "type": "function",
                "function": {"name": name, "arguments": args},
            })
        delta = {"role": "assistant", "tool_calls": openai_calls}
        chunk = {
            **template_obj,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        # Strip template choices/role — caller already set them above.
        chunk.pop("choices", None)
        chunk = {
            "id": template_obj.get("id") or f"chatcmpl-{secrets.token_hex(14)}",
            "object": "chat.completion.chunk",
            "model": template_obj.get("model") or "tusker-gateway",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    async for chunk in raw_stream:
        buffer += chunk
        while b"\n\n" in buffer:
            frame, buffer = buffer.split(b"\n\n", 1)
            frame += b"\n\n"  # preserve terminator for yield
            stripped = frame.strip()
            # Pass through non-data frames as-is (comments, empty)
            if not stripped.startswith(b"data: "):
                yield frame
                continue
            # Pass through [DONE] sentinel as-is
            if stripped == b"data: [DONE]":
                tool_stripper.flush()
                saw_done = True
                # Emit any pending blocks the upstream never got to close
                # before [DONE]. If we accumulated an unclosed block, drop
                # it (it never completed) but still pass [DONE] through.
                yield frame
                continue
            try:
                obj = json.loads(stripped[len(b"data: "):])
            except (json.JSONDecodeError, UnicodeDecodeError):
                yield frame
                continue
            choices = obj.get("choices")
            if not isinstance(choices, list) or not choices:
                yield frame
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            fr = choice.get("finish_reason")
            if fr:
                saw_finish_reason = True
            raw_content = delta.get("content")
            reasoning_content = delta.get("reasoning_content")
            # Some reasoning models (qwen, etc.) emit reasoning/thinking in
            # `reasoning_content` while leaving `content` null or empty.
            # OMP treats content=null as "no text" and ends the turn early.
            # Promote reasoning_content to content when content is absent so
            # clients see real text and keep the conversation alive.
            if raw_content is None and reasoning_content is not None:
                raw_content = reasoning_content
                delta["content"] = reasoning_content
                # OMP client compatibility: sometimes reasoning models omit "content"
                # when "reasoning_content" is present.
                if "reasoning_content" in delta:
                    del delta["reasoning_content"]
            has_content = isinstance(raw_content, str) and bool(raw_content)
            tc = delta.get("tool_calls")
            has_tools = bool(tc) and isinstance(tc, list) and len(tc) > 0
            # Strip XML/Markdown tool-call markup from content deltas using
            # a stateful stripper so markup spanning multiple streamed chunks
            # is still recognized and dropped. We always call feed() when
            # there's content — even if this chunk contains no `<` — so the
            # stripper's carry (a partial opener from the previous chunk)
            # gets reassembled with this chunk's content.
            if has_content:
                cleaned = tool_stripper.feed(raw_content)
                if not cleaned and has_tools:
                    delta = {k: v for k, v in delta.items() if k != "content"}
                else:
                    delta = {**delta, "content": cleaned}
            # Replace delta in choice and obj for downstream re-emission.
            new_choice = {**choice, "delta": delta}
            new_obj = {**obj, "choices": [new_choice, *choices[1:]]}
            # Split if chunk has BOTH content/tools AND finish_reason
            if fr and (bool(delta.get("content")) or has_tools):
                if bool(delta.get("content")):
                    content_delta = {k: v for k, v in delta.items()
                                   if k not in ("role", "tool_calls")}
                    content_obj = {**new_obj, "choices": [{**new_choice, "delta": content_delta, "finish_reason": None}]}
                    yield f"data: {json.dumps(content_obj, ensure_ascii=False)}\n\n".encode()
                if has_tools:
                    tools_only = {"role": delta.get("role"), "tool_calls": tc}
                    tools_obj = {**new_obj, "choices": [{**new_choice, "delta": tools_only, "finish_reason": None}]}
                    yield f"data: {json.dumps(tools_obj, ensure_ascii=False)}\n\n".encode()
                finish_obj = {**new_obj, "choices": [{**new_choice, "delta": {}, "finish_reason": fr}]}
                yield f"data: {json.dumps(finish_obj, ensure_ascii=False)}\n\n".encode()
            else:
                yield f"data: {json.dumps(new_obj, ensure_ascii=False)}\n\n".encode()
            # After emitting the cleaned content frame, drain any complete
            # bare <function=...>...</function> blocks the stripper
            # collected while processing this chunk.
            #
            # Well-formed blocks (those with at least one
            # <parameter=k>v</parameter> sibling) are promoted into a
            # structured delta.tool_calls frame so OMP receives a real
            # tool_calls array.
            #
            # Malformed blocks (model wrote prose inside an unclosed
            # function block, or emitted a function block with no
            # arguments) are dropped from the tool_calls promotion, but
            # their inner prose between <function=...> and </function> is
            # extracted and emitted as ordinary text content so OMP still
            # sees what the model actually said.
            for block, had_params in tool_stripper.drain_pending_blocks():
                if had_params:
                    parsed = parse_text_tool_calls(block)
                    tool_frame = _tool_calls_frame(parsed, new_obj)
                    if tool_frame is not None:
                        yield tool_frame
                else:
                    prose = _extract_inner_prose(block)
                    if prose:
                        # Emit as a content frame after the (already-emitted)
                        # main content frame, so OMP renders it inline.
                        yield _content_frame(prose, new_obj)
    # Flush any remaining partial frame in the buffer
    if buffer.strip():
        yield buffer
    # If the upstream ended without ever emitting a finish_reason, OMP
    # surfaces this as "stream closed before a finish_reason was received".
    # Synthesize a stop chunk so the client always has a clean termination.
    # Skip if the upstream already sent its own [DONE] (which implies it
    # terminated cleanly) to avoid emitting a chunk after the sentinel.
    if not saw_finish_reason and not saw_done:
        yield _finish_frame()


def _pool_name(body: dict[str, Any]) -> str | None:
    route = resolve_route(body.get("model"), body)
    return route.pool_name or "code" if route.kind in {"pool", "code"} else None


def _resolve_api_key(request: web.Request) -> str:
    """Return the raw bearer token used by the caller (for budget keying)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return ""


def _estimated_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough prompt-token estimate for budget pre-flight.

    We don't have tiktoken in this image, so we use a coarse char-based
    estimate (1 token ~= 4 chars). The pre-flight is intentionally
    conservative — over-budgeting a request by a few hundred tokens is
    fine, under-budgeting causes a 429 after the provider call which is
    worse.
    """
    chars = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    chars += len(str(part.get("text", "")))
    return max(1, chars // 4)


# Request fields handled separately by the gateway. Everything else from
# the request body is forwarded upstream as-is so callers can pass
# max_tokens / temperature / top_p / stop / seed / response_format / etc.
# without us needing to maintain a whitelist. Without this passthrough,
# upstream providers fall back to their own tiny defaults (often 256-512
# tokens) and the model silently truncates mid-task with a StopReason
# of 'length' that OMP then interprets as 'finished'.
_GATEWAY_HANDLED_FIELDS = frozenset({
    "model", "messages", "stream", "tools", "tool_choice",
})


def _build_extra_body(body: dict[str, Any]) -> dict[str, Any]:
    """Extract passthrough fields from a request body.

    Returns a dict of fields that should be forwarded to the upstream
    provider as `extra_body`, excluding fields the gateway already
    handles (model/messages/stream/tools/tool_choice).

    Modern OpenAI clients send `max_completion_tokens` while older
    providers and the codex Responses API only support `max_tokens`.
    Map the newer name to the older one so requests don't get rejected.
    """
    extra = {k: v for k, v in body.items() if k not in _GATEWAY_HANDLED_FIELDS}
    if "max_completion_tokens" in extra and "max_tokens" not in extra:
        extra["max_tokens"] = extra.pop("max_completion_tokens")
    return extra

def _cooldown_for_exc(exc: GatewayError) -> float | None:
    """Derive a circuit-breaker cooldown from a provider error.

    Returns the backoff seconds (e.g. a long value for quota-exhaustion or a
    permanent 401/403/404) or ``None`` when it cannot be determined / the
    error is transient, so the breaker falls back to its policy cooldown.
    """
    from tusker_gateway.cooldown import (
        _cooldown_seconds_for_429,
        _cooldown_seconds_for_provider_error,
    )

    try:
        if isinstance(exc, RateLimitError):
            return _cooldown_seconds_for_429(
                {"body": exc.body or "", "headers": exc.headers}
            )
        return _cooldown_seconds_for_provider_error(exc)
    except Exception:
        return None

def _mark_permanently_failed(
    exc: Exception,
    provider: str,
    model: str,
) -> None:
    """Record a (provider, model) that returned a permanent 401/403.

    The auto-free pool extension consults this to skip/prune genuinely-dead
    models (agentic-harness-only, WAF-blocked, wrong-tier) so they don't
    re-enter rotation and keep failing. Transient 5xx / 429 are NOT marked.
    """
    status = getattr(exc, "upstream_status", None)
    if status in (401, 403):
        from tusker_gateway.cooldown import mark_permanently_failed

        mark_permanently_failed(provider, model)

def _clear_permanently_failed(provider: str, model: str) -> None:
    """Clear a permanent-failure marker once a provider/model recovers."""
    from tusker_gateway.cooldown import clear_permanently_failed

    clear_permanently_failed(provider, model)

_IMAGE_INPUT_MODALITIES = frozenset({"image"})


def _required_input_modalities(messages: Any) -> frozenset[str] | None:
    """Return pool capabilities required by OpenAI-format messages."""
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        if any(
            isinstance(block, dict) and block.get("type") in {"image_url", "input_image"}
            for block in message["content"]
        ):
            return _IMAGE_INPUT_MODALITIES
    return None


async def _call_with_pool_fallback(
    config: dict[str, Any],
    body: dict[str, Any],
    client: PassthroughClient,
    tools: list[dict[str, Any]] | None = None,
    breaker: CircuitBreaker | None = None,
    request: web.Request | None = None,
    metrics_registry: Any | None = None,
) -> tuple[str, str, Any]:
    """Call a pool candidate, trying the next candidate after provider failure.

    If `breaker` is set, candidates whose breaker is OPEN are skipped before
    the call is attempted. Successful calls record success; failures (other
    than 429 rate-limit, which uses the cooldown path) record failure.
    """
    extra_body = _build_extra_body(body)
    required_input_modalities = _required_input_modalities(body.get("messages"))
    pool_name = _pool_name(body)
    if pool_name is None:
        provider, model = _route_target(config, body)
        decision = breaker.check(provider, model) if breaker else BreakerDecision(allowed=True, state=None)
        if not decision.allowed:
            raise BadRequestError(
                f"circuit open for {provider}/{model}: {decision.reason}",
                code="circuit_open",
            )
        try:
            result = await client.chat(
                provider, model, body["messages"],
                stream=bool(body.get("stream")),
                tools=tools,
                extra_body=extra_body or None,
                metrics_registry=metrics_registry,
            )
            if breaker is not None:
                breaker.record_success(provider, model)
            _clear_permanently_failed(provider, model)
            return provider, model, result
        except RateLimitError as exc:
            if breaker is not None:
                breaker.record_failure(
                    provider,
                    model,
                    cooldown_secs=_cooldown_for_exc(exc),
                )
            raise
        except Exception as exc:
            if breaker is not None:
                breaker.record_failure(
                    provider,
                    model,
                    cooldown_secs=(_cooldown_for_exc(exc) if isinstance(exc, GatewayError) else None),
                )
            _mark_permanently_failed(exc, provider, model)
            raise

    excluded: set[tuple[str, str]] = set()
    last_error: Exception | None = None
    # Prefer the app-level PoolManager (so catalog refresh + session
    # stickiness are shared); fall back to a per-request instance.
    if request is not None:
        pool_mgr = request.app.get("pool_manager") or PoolManager(config)
    else:
        pool_mgr = PoolManager(config)
    while True:
        # Pool select() already filters out cooldown-blocked candidates;
        # additionally filter out breaker-open candidates here.
        selected = pool_mgr.select(
            pool_name,
            excluded=set(excluded),
            required_input_modalities=required_input_modalities,
        )
        if breaker is not None and selected is not None:
            while selected is not None:
                decision = breaker.check(selected[0], selected[1])
                if decision.allowed:
                    break
                excluded.add(selected)
                selected = pool_mgr.select(
                    pool_name,
                    excluded=set(excluded),
                    required_input_modalities=required_input_modalities,
                )
        if not selected:
            if last_error is not None:
                raise last_error
            raise BadRequestError("No healthy models in pool", code="no_healthy_models")
        provider, model = selected
        try:
            result = await client.chat(
                provider, model, body["messages"],
                stream=bool(body.get("stream")),
                tools=tools,
                extra_body=extra_body or None,
                metrics_registry=metrics_registry,
            )
            if breaker is not None:
                breaker.record_success(provider, model)
            _clear_permanently_failed(provider, model)
            return provider, model, result
        except RateLimitError as exc:
            if breaker is not None:
                breaker.record_failure(
                    provider,
                    model,
                    cooldown_secs=_cooldown_for_exc(exc),
                )
            last_error = exc
            excluded.add(selected)
        except Exception as exc:
            if breaker is not None:
                breaker.record_failure(
                    provider,
                    model,
                    cooldown_secs=(_cooldown_for_exc(exc) if isinstance(exc, GatewayError) else None),
                )
            _mark_permanently_failed(exc, provider, model)
            last_error = exc
            excluded.add(selected)


def _image_url_value(block: dict[str, Any], *, context: str) -> tuple[str, str | None]:
    image_url = block.get("image_url")
    detail = block.get("detail")
    if isinstance(image_url, dict):
        url = image_url.get("url")
        detail = image_url.get("detail", detail)
    else:
        url = image_url
    if not isinstance(url, str) or not url:
        raise BadRequestError(
            f"{context} must contain a non-empty image_url",
            code="invalid_image_url",
        )
    if detail is not None and not isinstance(detail, str):
        raise BadRequestError(
            f"{context} detail must be a string",
            code="invalid_image_url",
        )
    return url, detail


def _validate_message_content(content: Any, *, role: str, context: str) -> None:
    if isinstance(content, str):
        return
    if role == "tool":
        # Tool outputs are provider-defined JSON/text payloads. Preserve the
        # existing permissive behavior rather than treating them as media.
        return
    if not isinstance(content, list):
        raise BadRequestError(
            f"{context} content must be a string or array",
            code="invalid_content",
        )
    for block_index, block in enumerate(content):
        block_context = f"{context} content[{block_index}]"
        if not isinstance(block, dict):
            raise BadRequestError(
                f"{block_context} must be an object",
                code="invalid_content",
            )
        block_type = block.get("type")
        if not isinstance(block_type, str) or not block_type:
            raise BadRequestError(
                f"{block_context} must contain a type",
                code="invalid_content",
            )
        if block_type in {"text", "input_text"}:
            if not isinstance(block.get("text"), str):
                raise BadRequestError(
                    f"{block_context} must contain a string text field",
                    code="invalid_content",
                )
        elif block_type in {"image_url", "input_image"}:
            _image_url_value(block, context=block_context)


def _responses_content_to_chat(content: Any, *, context: str, role: str = "user") -> Any:
    if isinstance(content, str):
        return content
    if role == "tool":
        return content
    if not isinstance(content, list):
        raise BadRequestError(
            f"{context} must be a string or array",
            code="invalid_input",
        )

    converted: list[dict[str, Any]] = []
    for block_index, block in enumerate(content):
        block_context = f"{context}[{block_index}]"
        if not isinstance(block, dict):
            raise BadRequestError(
                f"{block_context} must be an object",
                code="invalid_input",
            )
        block_type = block.get("type")
        if block_type == "input_text":
            text = block.get("text")
            if not isinstance(text, str):
                raise BadRequestError(
                    f"{block_context} must contain a string text field",
                    code="invalid_input",
                )
            converted.append({"type": "text", "text": text})
        elif block_type == "input_image":
            url, detail = _image_url_value(block, context=block_context)
            image_url: dict[str, Any] = {"url": url}
            if detail is not None:
                image_url["detail"] = detail
            converted.append({"type": "image_url", "image_url": image_url})
        elif block_type == "text":
            if not isinstance(block.get("text"), str):
                raise BadRequestError(
                    f"{block_context} must contain a string text field",
                    code="invalid_input",
                )
            converted.append(dict(block))
        elif block_type == "image_url":
            _image_url_value(block, context=block_context)
            converted.append(dict(block))
        else:
            raise BadRequestError(
                f"Unsupported Responses content block type: {block_type}",
                code="unsupported_content_type",
            )
    return converted


def _responses_input_to_messages(input_value: Any) -> list[dict[str, Any]]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if not isinstance(input_value, list) or not input_value:
        raise BadRequestError("input must be a string or non-empty array", code="invalid_input")

    if all(isinstance(item, dict) and item.get("type") in {
        "input_text", "input_image", "text", "image_url",
    } for item in input_value):
        return [{
            "role": "user",
            "content": _responses_content_to_chat(input_value, context="input"),
        }]

    messages: list[dict[str, Any]] = []
    for item_index, item in enumerate(input_value):
        if not isinstance(item, dict):
            raise BadRequestError(
                f"input[{item_index}] must be a message object",
                code="invalid_input",
            )
        if item.get("type") not in {None, "message"}:
            raise BadRequestError(
                f"Unsupported Responses input item type: {item.get('type')}",
                code="unsupported_content_type",
            )
        role = item.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise BadRequestError(
                f"input[{item_index}] must have a valid message role",
                code="invalid_input",
            )
        if "content" not in item:
            raise BadRequestError(
                f"input[{item_index}] must contain content",
                code="invalid_input",
            )
        message = {key: value for key, value in item.items() if key != "type"}
        message["content"] = _responses_content_to_chat(
            item["content"],
            context=f"input[{item_index}].content",
            role=role,
        )
        messages.append(message)
    return messages


def _validate_chat_body(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BadRequestError("Request body must be a JSON object", code="invalid_request")
    if "messages" not in body:
        raise BadRequestError("messages is required", code="invalid_request")
    messages = body["messages"]
    if not isinstance(messages, list) or not messages:
        raise BadRequestError("messages must be a non-empty array", code="invalid_messages")
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise BadRequestError("Each message must have a valid role", code="invalid_messages")
        role = message["role"]
        if "content" not in message and role != "assistant":
            raise BadRequestError("Each message must contain content", code="invalid_messages")
        if "content" in message:
            _validate_message_content(
                message["content"],
                role=role,
                context=f"messages[{index}]",
            )
    if "stream" in body and not isinstance(body["stream"], bool):
        raise BadRequestError("stream must be a boolean", code="invalid_stream")
    return body


def _route_target(config: dict[str, Any], body: dict[str, Any]) -> tuple[str, str]:
    route = resolve_route(body.get("model"), body)
    if route.kind in {"pool", "code"}:
        selected = PoolManager(config).select(
            route.pool_name or "code",
            required_input_modalities=_required_input_modalities(body.get("messages")),
        )
        if not selected:
            raise BadRequestError("No healthy models in pool", code="no_healthy_models")
        return selected
    if route.kind == "passthrough" and route.provider and route.model:
        return route.provider, route.model
    raise BadRequestError("Unsupported model route", code="unsupported_route")


async def models_handler(request: web.Request) -> web.Response:
    """GET /v1/models — list available models."""
    config = request.app["config"]
    data = [{"id": config["model_name"], "object": "model", "owned_by": "tusker-gateway"}]
    data.extend({"id": alias, "object": "model", "owned_by": "tusker-gateway"} for alias in ("hermes-code", "hermes-privacy", "hermes-premium", "hermes-swarm"))
    return web.json_response({"object": "list", "data": data})


async def metrics_handler(request: web.Request) -> web.Response:
    """GET /metrics — Prometheus text exposition."""
    metrics: MetricsRegistry | None = request.app.get("metrics")
    if metrics is None:
        return web.Response(status=404, text="metrics not enabled\n")
    # Refresh gauges from live state.
    cache: ResponseCache | None = request.app.get("cache")
    if cache is not None:
        s = cache.stats_snapshot()
        metrics.cache_hits._values[()] = float(s["hits"])  # noqa: SLF001
        metrics.cache_misses._values[()] = float(s["misses"])
        metrics.cache_writes._values[()] = float(s["writes"])
        metrics.cache_evictions._values[()] = float(s["evictions"])
    budget: BudgetTracker | None = request.app.get("budget")
    if budget is not None:
        s = budget.stats_snapshot()
        for kind, value in (
            ("daily", s["blocks_daily"]),
            ("monthly", s["blocks_monthly"]),
            ("pool", s["blocks_pool"]),
            ("global_daily", s["blocks_global"]),
        ):
            metrics.budget_blocks._values[(kind,)] = float(value)  # noqa: SLF001
        metrics.budget_records._values[()] = float(s["records"])
        metrics.budget_refunds._values[()] = float(s["refunds"])
    breaker: CircuitBreaker | None = request.app.get("breaker")
    if breaker is not None:
        s = breaker.stats_snapshot()
        # Surface breaker stats via existing budget_blocks counter family so
        # we don't grow the metric catalogue. Reuse 'breaker' kind label.
        for kind in ("trips", "short_circuits", "half_open_probes", "half_open_successes", "half_open_failures"):
            metrics.budget_blocks._values[(f"breaker_{kind}",)] = float(s[kind])  # noqa: SLF001
    ratelimit: RateLimiter | None = request.app.get("ratelimit")
    if ratelimit is not None:
        s = ratelimit.stats_snapshot()
        metrics.budget_blocks._values[("ratelimit_allowed",)] = float(s["allowed"])  # noqa: SLF001
        metrics.budget_blocks._values[("ratelimit_blocked",)] = float(s["blocked"])  # noqa: SLF001
    sem_cache = request.app.get("semantic_cache")
    if sem_cache is not None and sem_cache.enabled:
        s = sem_cache.stats_snapshot()
        for kind, value in (
            ("hits", s["hits"]),
            ("misses", s["misses"]),
            ("writes", s["writes"]),
            ("evictions", s["evictions"]),
        ):
            metrics.budget_blocks._values[(f"semantic_{kind}",)] = float(value)  # noqa: SLF001
    body = metrics.render()
    return web.Response(
        status=200,
        body=body,
        headers={"Content-Type": MetricsRegistry.CONTENT_TYPE},
    )


async def chat_completions_handler(request: web.Request) -> web.Response | web.StreamResponse:
    """POST /v1/chat/completions."""
    metrics: MetricsRegistry | None = request.app.get("metrics")
    cache: ResponseCache | None = request.app.get("cache")
    sem_cache = request.app.get("semantic_cache")
    budget: BudgetTracker | None = request.app.get("budget")
    breaker: CircuitBreaker | None = request.app.get("breaker")
    ratelimit: RateLimiter | None = request.app.get("ratelimit")
    tracer: Tracer | None = request.app.get("tracer")

    started = time.monotonic()
    request_id = uuid.uuid4().hex[:12]
    pool_name = "passthrough"  # overwritten for pool-routed requests
    provider = "unknown"
    target_model = "unknown"
    status = "ok"
    body: dict[str, Any] | None = None
    api_key = _resolve_api_key(request)

    def _emit(status_label: str, provider_label: str | None = None,
              model_label: str | None = None) -> None:
        if metrics is None:
            return
        pl = provider_label if provider_label is not None else provider
        ml = model_label if model_label is not None else target_model
        metrics.requests_total.inc({"pool": pool_name, "provider": pl, "model": ml, "status": status_label})
        metrics.request_duration.observe(time.monotonic() - started, {"pool": pool_name, "provider": pl, "model": ml})

    # Top-level span (synchronous context).
    span_cm = (
        tracer.span("chat_completion", attributes={
            "http.method": request.method,
            "http.path": "/v1/chat/completions",
            "tusker.api_key_fingerprint": _resolve_api_key(request)[:16],
        })
        if tracer is not None and tracer.enabled
        else _noop_cm()
    )

    with span_cm as root_span:
        try:
            body = _validate_chat_body(await request.json())
            logger.info('chat request rid=%s model=%s pool=%s stream=%s', request_id, body.get("model"), pool_name, body.get("stream"))
            if tracer is not None and tracer.enabled and root_span is not None:
                root_span.attributes["tusker.model"] = str(body.get("model") or "")
            config = request.app["config"]
            client = PassthroughClient(config, QualityDB(config["quality_db_path"]), request.app["http_session"], catalog_registry=request.app.get("catalog_registry"))
            tools = body.get("tools") if isinstance(body.get("tools"), list) else None
            pool_name = _pool_name(body) or "passthrough"
            bypass_cache = request.headers.get("X-Tusker-Cache", "").strip().lower() == "bypass"

            # Guard pipeline: input/output guards.
            guard_pipeline = request.app.get("guard_pipeline")
            if guard_pipeline is not None:
                guard_result = await guard_pipeline.run(body)
                if not guard_result.allowed:
                    status = "guardrail_blocked"
                    if metrics is not None:
                        metrics.guardrail_blocks.inc({"kind": guard_result.message or "blocked"})
                    _emit(status)
                    return web.json_response(
                        openai_error(guard_result.message or "request blocked by guardrail", code="guardrail_blocked", error_type="invalid_request_error"),
                        status=400,
                    )
                if guard_result.modified_body is not None:
                    body = guard_result.modified_body


            # Rate-limit pre-flight (cheapest check, runs first).
            if ratelimit is not None and api_key:
                rl = ratelimit.check(api_key)
                if not rl.allowed:
                    status = "ratelimit_blocked"
                    if metrics is not None:
                        metrics.budget_blocks.inc({"kind": "ratelimit_blocked"})
                    _emit(status)
                    headers = {
                        "Retry-After": str(int(rl.retry_after) + 1),
                        "X-Tusker-RateLimit-Reason": rl.reason or "rate limit exceeded",
                    }
                    return web.json_response(
                        openai_error(rl.reason or "rate limit exceeded", code="rate_limit_error", error_type="rate_limit_error"),
                        status=429,
                        headers=headers,
                    )

            # Cache lookup
            cache_key: str | None = None
            if cache is not None and not body.get("stream", False) and not bypass_cache:
                cache_key = make_cache_key(
                    pool_name=pool_name,
                    model=body.get("model"),
                    messages=body["messages"],
                    tools=tools,
                    extra_body=body.get("extra_body"),
                )
                hit = cache.get(cache_key)
                if hit is not None:
                    logger.debug('cache hit key=%s', cache_key[:16])
                    if metrics is not None:
                        metrics.requests_total.inc(
                            {"pool": pool_name, "provider": "cache", "model": str(body.get("model") or ""), "status": "cache_hit"}
                        )
                        metrics.request_duration.observe(time.monotonic() - started, {"pool": pool_name, "provider": "cache", "model": str(body.get("model") or "")})
                    return web.json_response(hit)

            # Semantic cache lookup (after exact-match miss).
            sem_hit: dict[str, Any] | None = None
            if (
                sem_cache is not None
                and sem_cache.enabled
                and not body.get("stream", False)
                and not bypass_cache
            ):
                sem_hit = await sem_cache.query(body["messages"])
                if sem_hit is not None:
                    logger.info('semantic cache hit model=%s', body.get("model"))
                    if metrics is not None:
                        metrics.requests_total.inc(
                            {"pool": pool_name, "provider": "semantic_cache", "model": str(body.get("model") or ""), "status": "cache_hit"}
                        )
                        metrics.request_duration.observe(time.monotonic() - started, {"pool": pool_name, "provider": "semantic_cache", "model": str(body.get("model") or "")})
                    # Also store in exact-match cache for faster subsequent lookups.
                    if cache is not None and cache_key is not None:
                        cache.put(cache_key, sem_hit)
                    return web.json_response(sem_hit)

            # Budget pre-flight
            if budget is not None and api_key:
                est = _estimated_tokens(body["messages"])
                decision = budget.check(api_key, pool_name, est)
                if not decision.allowed:
                    status = "budget_blocked"
                    if metrics is not None:
                        metrics.budget_blocks.inc({"kind": decision.cap_name or "unknown"})
                    _emit(status)
                    headers = {"X-Tusker-Budget-Reason": decision.reason or "budget exceeded"}
                    return web.json_response(
                        openai_error(decision.reason or "budget exceeded", code="budget_exceeded", error_type="rate_limit_error"),
                        status=429,
                        headers=headers,
                    )

            provider, target_model, result = await _call_with_pool_fallback(
                config, body, client, tools,
                breaker=breaker, request=request,
                metrics_registry=request.app.get("metrics"),
            )
            logger.debug('selected rid=%s provider=%s model=%s pool=%s', request_id, provider, target_model, pool_name)

            if budget is not None and api_key and isinstance(result, dict):
                usage = result.get("usage") or {}
                used = int(usage.get("total_tokens") or _estimated_tokens(body["messages"]))
                budget.record(api_key, pool_name, used)

            if (
                cache is not None
                and not bypass_cache
                and not body.get("stream", False)
                and isinstance(result, dict)
                and cache_key is not None
            ):
                cache.put(cache_key, result)
                logger.debug('cache stored key=%s', cache_key[:16])

            # Store in semantic cache (non-streaming dict responses only).
            if (
                sem_cache is not None
                and sem_cache.enabled
                and not bypass_cache
                and not body.get("stream", False)
                and isinstance(result, dict)
            ):
                await sem_cache.store(body["messages"], result)
                logger.debug('semantic cache stored model=%s', body.get("model"))

            if body.get("stream", False):
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        # Disable nginx-style response buffering so SSE events
                        # flush immediately. Traefik honors this too.
                        "X-Accel-Buffering": "no",
                    },
                )
                await resp.prepare(request)

                # Send the role chunk *before* the first upstream byte. This
                # (a) gives the client a parseable first event immediately, and
                # (b) forces the first bytes through any proxy buffer so
                # subsequent heartbeats aren't held back. OpenAI's reference
                # streaming behavior starts with `delta: {role: "assistant"}`.
                await resp.write(sse_frame(format_openai_chunk(role="assistant")))

                stop = asyncio.Event()
                hb_interval = _sse_heartbeat_secs()
                hb_task = asyncio.create_task(
                    sse_heartbeat_loop(
                        resp.write,
                        stop,
                        interval_secs=hb_interval,
                        comment="keepalive",
                    ),
                    name="sse-heartbeat",
                )
                stream_ok = True
                try:
                    if isinstance(result, dict):
                        # Convert complete response to SSE chunks.
                        # Codex parses the full response from its SSE stream;
                        # the gateway receives it as a single dict and must
                        # emit it as proper OpenAI streaming chunks so the
                        # client (e.g. OMP) can consume them as text deltas.
                        choices = result.get("choices", [{}])
                        choice = choices[0]
                        message = choice.get("message", {})
                        content = message.get("content", "")
                        finish_reason = choice.get("finish_reason", "stop")
                        
                        # Emit content chunk if there's text
                        if content:
                            await resp.write(sse_frame(format_openai_chunk(content=content)))
                        
                        # Emit tool_calls as individual deltas if present
                        tool_calls = message.get("tool_calls")
                        if tool_calls:
                            for tc in tool_calls:
                                tc_id = tc.get("id", "")
                                fn = tc.get("function", {})
                                tc_delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": tc_id, "type": "function", "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments", "")}}]}
                                await resp.write(sse_frame({"id": result.get("id", "chatcmpl-tusker"), "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": tc_delta}], "model": result.get("model", "tusker-gateway")}))
                        
                        # Emit finish_reason chunk (distinct from content to satisfy OMP)
                        await resp.write(sse_frame(format_openai_chunk(finish_reason=finish_reason)))
                    else:
                        async for chunk in _normalize_stream(result):
                            await resp.write(chunk)
                except (ConnectionResetError, ConnectionError, BrokenPipeError) as exc:
                    stream_ok = False
                    logger.info(
                        "stream client disconnected mid-flight rid=%s provider=%s model=%s err=%s",
                        request_id, provider, target_model, exc,
                    )
                    if budget is not None and api_key and body is not None:
                        budget.refund(api_key, pool_name, _estimated_tokens(body["messages"]))
                except asyncio.CancelledError:
                    stream_ok = False
                    logger.info(
                        "stream cancelled rid=%s provider=%s model=%s", request_id, provider, target_model,
                    )
                    raise
                except Exception as exc:  # noqa: BLE001
                    stream_ok = False
                    logger.warning(
                        "stream pump failed rid=%s provider=%s model=%s err=%s",
                        request_id, provider, target_model, exc,
                        exc_info=True,
                    )
                    if budget is not None and api_key and body is not None:
                        budget.refund(api_key, pool_name, _estimated_tokens(body["messages"]))
                else:
                    # Best-effort: if the client is gone, [DONE] write will
                    # raise — swallow so we still record metrics.
                    try:
                        await resp.write(sse_done())
                    except (ConnectionResetError, ConnectionError, BrokenPipeError):
                        stream_ok = False
                finally:
                    stop.set()
                    try:
                        await asyncio.wait_for(hb_task, timeout=hb_interval + 1.0)
                    except asyncio.TimeoutError:
                        hb_task.cancel()
                status = "ok" if stream_ok else status
                _emit(status)
                return resp

            _emit(status)
            if metrics is not None:
                usage = (result or {}).get("usage") or {} if isinstance(result, dict) else {}
                for direction, key in (("prompt", "prompt_tokens"), ("completion", "completion_tokens")):
                    n = int(usage.get(key) or 0)
                    if n:
                        metrics.tokens_total.inc({"pool": pool_name, "provider": provider, "model": target_model, "direction": direction}, n)
            return web.json_response(result)
        except BadRequestError as exc:
            status = exc.code or "bad_request"
            _emit(status)
            return web.json_response(openai_error(exc.message, code=exc.code, error_type=exc.error_type), status=exc.status)
        except Exception as exc:
            logger.warning('chat request failed rid=%s: %s', request_id, exc)
            status = "provider_error"
            _emit(status)
            if budget is not None and api_key and body is not None:
                budget.refund(api_key, pool_name, _estimated_tokens(body["messages"]))
            return web.json_response(openai_error(str(exc), code="provider_error", error_type="provider_error"), status=502)


async def responses_handler(request: web.Request) -> web.Response | web.StreamResponse:
    try:
        body = await request.json()
        logger.info('responses request model=%s', body.get("model") if isinstance(body, dict) else None)
        if not isinstance(body, dict):
            raise BadRequestError("Request body must be a JSON object", code="invalid_request")
        messages = _responses_input_to_messages(body.get("input"))
        chat_body = _validate_chat_body({
            "model": body.get("model"),
            "messages": messages,
            "stream": bool(body.get("stream", False)),
        })
        config = request.app["config"]
        client = PassthroughClient(config, QualityDB(config["quality_db_path"]), request.app["http_session"], catalog_registry=request.app.get("catalog_registry"))
        _, _, result = await _call_with_pool_fallback(
            config, chat_body, client, request=request,
            metrics_registry=request.app.get("metrics"),
        )
        if isinstance(result, dict) and "choices" in result:
            text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        else:
            text = ""
        resp_obj = {"id": f"resp_{uuid.uuid4().hex}", "object": "response", "created_at": int(time.time()), "model": body.get("model") or config["model_name"], "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}], "status": "completed"}
        return web.json_response(resp_obj)
    except BadRequestError as exc:
        return web.json_response(openai_error(exc.message, code=exc.code, error_type=exc.error_type), status=exc.status)
    except Exception as exc:
        return web.json_response(openai_error(str(exc), code="provider_error", error_type="provider_error"), status=502)


class _NoOpCM:
    """Null context manager that yields None — used when tracing is disabled."""

    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False


def _noop_cm():
    return _NoOpCM()


async def _media_preflight(
    request: web.Request,
    body: dict[str, Any],
    *,
    budget_units: int,
) -> web.Response | None:
    """Apply the shared auth-key controls before expensive media calls."""
    api_key = _resolve_api_key(request)
    ratelimit: RateLimiter | None = request.app.get("ratelimit")
    if ratelimit is not None and api_key:
        decision = ratelimit.check(api_key)
        if not decision.allowed:
            return web.json_response(
                openai_error(
                    decision.reason or "rate limit exceeded",
                    code="rate_limit_error",
                    error_type="rate_limit_error",
                ),
                status=429,
                headers={
                    "Retry-After": str(int(decision.retry_after) + 1),
                    "X-Tusker-RateLimit-Reason": decision.reason
                    or "rate limit exceeded",
                },
            )

    budget: BudgetTracker | None = request.app.get("budget")
    if budget is not None and api_key:
        decision = budget.check(api_key, "media", budget_units)
        if not decision.allowed:
            return web.json_response(
                openai_error(
                    decision.reason or "budget exceeded",
                    code="budget_exceeded",
                    error_type="rate_limit_error",
                ),
                status=429,
                headers={
                    "X-Tusker-Budget-Reason": decision.reason or "budget exceeded"
                },
            )

    guard_pipeline = request.app.get("guard_pipeline")
    if guard_pipeline is not None:
        guard_result = await guard_pipeline.run(body)
        if not guard_result.allowed:
            return web.json_response(
                openai_error(
                    guard_result.message or "request blocked by guardrail",
                    code="guardrail_blocked",
                    error_type="invalid_request_error",
                ),
                status=400,
            )
        if (
            guard_result.modified_body is not None
            and guard_result.modified_body is not body
        ):
            body.clear()
            body.update(guard_result.modified_body)
    return None


def _record_media_budget(
    request: web.Request,
    *,
    budget_units: int,
) -> None:
    budget: BudgetTracker | None = request.app.get("budget")
    api_key = _resolve_api_key(request)
    if budget is not None and api_key:
        budget.record(api_key, "media", budget_units)


async def images_handler(request: web.Request) -> web.Response:
    """POST /v1/images/generations, /v1/images/edits, /v1/images/variations.

    Image generation endpoint for OpenAI GPT Image models and other providers.
    Delegates to the ImageGenerationHandler for routing and processing.
    """
    try:
        body = await request.json()
        budget_units = 4096
        blocked = await _media_preflight(request, body, budget_units=budget_units)
        if blocked is not None:
            return blocked
        model = body.get("model", "gpt-image-2")

        image_handler = request.app.get("image_handler")
        if image_handler is None:
            return web.json_response(
                openai_error("image handler not initialised", code="internal_error", error_type="internal"),
                status=503,
            )

        provider = image_handler.get_provider_for_image_request(model, request.path)
        config = request.app["config"]
        provider_keys = config.get("provider_api_keys", {})
        api_key = provider_keys.get(provider)

        codex_rotator = request.app.get("codex_rotator")
        result = await image_handler.handle_request(
            model=model,
            path=request.path,
            body=body,
            api_key=api_key,
            codex_rotator=codex_rotator,
        )
        _record_media_budget(request, budget_units=budget_units)
        return web.json_response(result)

    except GatewayError as exc:
        logger.warning("Image generation request failed: %s", exc)
        return web.json_response(
            openai_error(exc.message, code=exc.code, error_type=exc.error_type),
            status=_media_error_status(exc),
        )
    except Exception as exc:
        logger.exception("Unexpected image generation failure")
        return web.json_response(
            openai_error(str(exc), code="image_generation_error", error_type="provider_error"),
            status=502,
        )


async def tts_handler(request: web.Request) -> web.Response:
    """POST /v1/audio/speech and return upstream-generated binary audio."""
    try:
        body = await request.json()
        model = body.get("model", "tts-1")
        tts = request.app.get("tts_handler")
        if tts is None:
            return web.json_response(
                openai_error("tts handler not initialised", code="internal_error", error_type="internal"),
                status=503,
            )
        config = request.app["config"]
        provider_keys = config.get("provider_api_keys", {})
        provider = tts.get_provider_for_tts_request(model)
        api_key = provider_keys.get(provider)
        audio_bytes, content_type = await tts.handle_request(
            model=model,
            body=body,
            api_key=api_key,
        )
        return web.Response(body=audio_bytes, content_type=content_type)
    except Exception as exc:
        logger.warning("TTS request failed: %s", exc)
        return web.json_response(
            openai_error(str(exc), code="tts_error", error_type="provider_error"),
            status=502,
        )


async def video_handler(request: web.Request) -> web.Response:
    """POST /v1/videos.

    Video generation endpoint. Returns a JSON job object. When wait=true
    (default) the gateway polls the upstream until the job completes and
    includes the rendered MP4 as base64 under b64_json. Set wait=false to
    get the initial job object immediately.
    """
    try:
        body = await request.json()
        budget_units = 32768
        blocked = await _media_preflight(request, body, budget_units=budget_units)
        if blocked is not None:
            return blocked
        model = body.get("model", "sora-2")
        wait = _truthy(request.query.get("wait", "true"))
        video = request.app.get("video_handler")
        if video is None:
            return web.json_response(
                openai_error("video handler not initialised", code="internal_error", error_type="internal"),
                status=503,
            )
        config = request.app["config"]
        provider_keys = config.get("provider_api_keys", {})
        provider = video.get_provider_for_video_request(model)
        api_key = provider_keys.get(provider)
        result = await video.handle_request(
            model=model,
            body=body,
            api_key=api_key,
            wait=wait,
        )
        _record_media_budget(request, budget_units=budget_units)
        return web.json_response(result)
    except GatewayError as exc:
        logger.warning("Video request failed: %s", exc)
        return web.json_response(
            openai_error(exc.message, code=exc.code, error_type=exc.error_type),
            status=_media_error_status(exc),
        )
    except Exception as exc:
        logger.exception("Unexpected video request failure")
        return web.json_response(
            openai_error(str(exc), code="video_error", error_type="provider_error"),
            status=502,
        )


def _media_error_status(exc: GatewayError) -> int:
    if exc.code in {
        "bad_request",
        "invalid_request",
        "missing_prompt",
        "unsupported_endpoint",
        "unsupported_model",
        "unsupported_parameter",
        "unsupported_provider",
    }:
        return 400
    if exc.code == "missing_api_key":
        return 503
    if exc.code == "timeout":
        return 504
    if exc.code == "upstream_error":
        return 502
    return exc.status


def _truthy(value: str) -> bool:
    """Parse a query-string bool. False for anything other than 1/true/yes/on."""
    return value.lower() in ("1", "true", "yes", "on")
