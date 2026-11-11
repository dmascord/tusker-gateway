"""Classify streamed content as visible text or leaked in-band reasoning.

Several upstreams stream their chain of thought in the OpenAI ``content``
channel, wrapped in the provider's own reasoning tags, instead of using the
``reasoning_content`` channel. MiniMax-M3 is the one seen in production here:
it opens with ``<think>`` and, when the output budget runs out inside the
thinking, never closes the tag and never emits a visible answer.

OMP heals that leak client-side into a thinking block, and a thinking block is
not a deliverable turn: OMP's ``assistantTurnDelivered`` counts only text, tool
calls, and images. A turn that ends with ``finish_reason=length`` and nothing
else therefore reaches OMP as "nothing delivered", and OMP retries the
identical request until it gives up ("Length-stop recovery gave up").

The gateway has to reach the same conclusion before it hands the terminal over,
so it needs the same classification. This module mirrors the tag vocabulary of
OMP's ``ThinkingInbandScanner`` closely enough to agree on the outcome. It is
deliberately stateful: a tag can be split across stream frames.

The classification never changes the bytes the gateway forwards. It only
decides whether the client is left with something it can show.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _ThinkingTag:
    """One in-band reasoning delimiter."""

    open: str
    close: str
    #: A bare close with no open means the provider template prefilled the
    #: opener into the prompt (DeepSeek-R1, Qwen3-Thinking), so the text in
    #: front of it was reasoning.
    implied_close: bool = False


# The tags OMP recognises, in the order it scans for them.
_THINKING_TAGS: tuple[_ThinkingTag, ...] = (
    # deepseek, glm, hermes, kimi, qwen3.
    _ThinkingTag("<think>", "</think>", implied_close=True),
    # anthropic, minimax, xml.
    _ThinkingTag("<thinking>", "</thinking>"),
    # anthropic.
    _ThinkingTag("<scratchpad>", "</scratchpad>"),
    # gemma fenced thinking.
    _ThinkingTag("```thinking\n", "```"),
)

_ALL_DELIMITERS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [tag.open for tag in _THINKING_TAGS] + [tag.close for tag in _THINKING_TAGS]
    )
)

_MAX_DELIMITER_LENGTH = max(len(delimiter) for delimiter in _ALL_DELIMITERS)


def _partial_overlap(text: str, delimiters: tuple[str, ...]) -> int:
    """Length of the longest suffix of *text* that prefixes a delimiter.

    The suffix is held back so a tag split across stream frames is still
    recognised once the rest of it arrives.
    """
    tail = text[-(_MAX_DELIMITER_LENGTH - 1):]
    best = 0
    for delimiter in delimiters:
        limit = min(len(tail), len(delimiter) - 1)
        for size in range(limit, best, -1):
            if tail.endswith(delimiter[:size]):
                best = size
                break
    return best


class InbandThinkingTracker:
    """Track how much streamed content is visible text rather than reasoning.

    ``feed`` classifies one chunk and returns the part of it the client will
    render as ordinary text. Read ``has_visible_text`` once the turn is
    finished: a bare closing tag with no opener can retroactively reclassify
    earlier text as reasoning.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._close_tag: str | None = None
        self._opened_tags: set[str] = set()
        self._visible = False
        self._other_content = False

    def note_other_content(self) -> None:
        """Record native reasoning or a tool call seen next to the text.

        OMP reclassifies a bare closing tag as reasoning only when the message
        holds nothing but that one text block. Native reasoning or a tool call
        means the message holds more, so the tag stays a stray.
        """
        self._other_content = True

    @property
    def has_visible_text(self) -> bool:
        """Whether the client is left with text it can show."""
        return self._visible

    def feed(self, text: str) -> str:
        """Classify *text* and return the part that is visible text."""
        if not text:
            return ""
        self._buffer += text
        visible: list[str] = []
        while True:
            if self._close_tag is not None:
                index = self._buffer.find(self._close_tag)
                if index < 0:
                    hold = _partial_overlap(self._buffer, (self._close_tag,))
                    self._buffer = self._buffer[len(self._buffer) - hold:]
                    break
                self._buffer = self._buffer[index + len(self._close_tag):]
                self._close_tag = None
                continue
            hit = self._find_tag(self._buffer)
            if hit is None:
                hold = _partial_overlap(self._buffer, _ALL_DELIMITERS)
                visible.append(self._buffer[: len(self._buffer) - hold])
                self._buffer = self._buffer[len(self._buffer) - hold:]
                break
            index, tag, is_opener = hit
            if not is_opener:
                if self._other_content:
                    # A stray tag: drop it and keep the text visible.
                    visible.append(self._buffer[:index])
                else:
                    # The provider prefilled the opener into the prompt, so
                    # everything in front of the bare close was reasoning.
                    self._visible = False
                self._buffer = self._buffer[index + len(tag.close):]
                continue
            visible.append(self._buffer[:index])
            self._buffer = self._buffer[index + len(tag.open):]
            self._close_tag = tag.close
            self._opened_tags.add(tag.open)
        result = "".join(visible)
        if result.strip():
            self._visible = True
        return result

    def _find_tag(self, buffer: str) -> tuple[int, _ThinkingTag, bool] | None:
        """Find the earliest reasoning tag in *buffer*.

        An opener starts a thinking section. A bare closer only counts while
        its opener has not been seen, because the provider template prefilled
        it in that case. The third element says whether the hit was an opener.
        """
        best: tuple[int, _ThinkingTag, bool] | None = None
        for tag in _THINKING_TAGS:
            index = buffer.find(tag.open)
            if index < 0:
                continue
            if best is None or index < best[0]:
                best = (index, tag, True)
        if best is not None:
            return best
        for tag in _THINKING_TAGS:
            if not tag.implied_close or tag.open in self._opened_tags:
                continue
            index = buffer.find(tag.close)
            if index < 0:
                continue
            if best is None or index < best[0]:
                best = (index, tag, False)
        return best


def has_visible_text(content: str) -> bool:
    """Return whether a complete *content* string holds client-visible text.

    The non-streaming counterpart of :class:`InbandThinkingTracker`.
    """
    tracker = InbandThinkingTracker()
    tracker.feed(content)
    return tracker.has_visible_text
