"""Unit tests for the in-band thinking classifier.

The gateway has to agree with OMP about whether a turn delivered anything the
client can show. OMP heals reasoning that leaked into the ``content`` channel
into a thinking block, and a turn holding only thinking is not deliverable: the
client retries the identical request instead of using it.

The rules encoded here mirror OMP's ``ThinkingInbandScanner``:

* A reasoning opener starts a thinking section; the text inside it is not
  visible.
* A bare closing tag with no opener means the provider template prefilled the
  opener into the prompt, so the text in front of it was reasoning.
* Text after a closing tag is visible again.
* OMP only reclassifies when the message holds nothing but that one text
  block. Native reasoning or a tool call next to the text means the message
  holds more, so the text survives.
"""
from __future__ import annotations

import pytest

from tusker_gateway.inband_thinking import InbandThinkingTracker, has_visible_text


@pytest.mark.parametrize(
    "content,visible",
    [
        # Plain prose is visible.
        ("The answer is 42.", True),
        ("", False),
        ("   \n\t ", False),
        # An explicit thinking section hides everything inside it.
        ("<think>reasoning</think>", False),
        ("<think>reasoning</think>The answer is 42.", True),
        ("<thinking>reasoning</thinking>The answer is 42.", True),
        ("<scratchpad>reasoning</scratchpad>The answer is 42.", True),
        # A prefilled opener leaves a bare close: the text in front of it was
        # reasoning, the text behind it is the answer.
        ("reasoning</think>", False),
        ("reasoning</think>The answer is 42.", True),
        # An unclosed thinking section never yields visible text.
        ("<think>reasoning that never ends", False),
    ],
)
def test_has_visible_text(content: str, visible: bool) -> None:
    assert has_visible_text(content) is visible


def test_a_reasoning_tag_split_across_frames_is_recognised() -> None:
    tracker = InbandThinkingTracker()
    for chunk in ("<thi", "nk>reasoning", "</thi", "nk>The answer is 42."):
        tracker.feed(chunk)
    assert tracker.has_visible_text is True


def test_an_unclosed_thinking_section_yields_no_visible_text() -> None:
    tracker = InbandThinkingTracker()
    for chunk in ("<think", ">reasoning that ", "never ends"):
        tracker.feed(chunk)
    assert tracker.has_visible_text is False


def test_native_reasoning_keeps_a_stray_close_tag_from_hiding_the_text() -> None:
    """OMP only reclassifies when the text block stands alone."""
    tracker = InbandThinkingTracker()
    tracker.note_other_content()
    tracker.feed("The answer is 42.")
    tracker.feed("</think>")
    assert tracker.has_visible_text is True


def test_a_tool_call_keeps_a_stray_close_tag_from_hiding_the_text() -> None:
    tracker = InbandThinkingTracker()
    tracker.note_other_content()
    tracker.feed("The answer is 42.")
    tracker.feed("</think>")
    assert tracker.has_visible_text is True


def test_a_bare_close_alone_hides_the_text_in_front_of_it() -> None:
    tracker = InbandThinkingTracker()
    tracker.feed("The answer is 42.")
    tracker.feed("</think>")
    assert tracker.has_visible_text is False


def test_text_after_a_bare_close_is_visible_again() -> None:
    tracker = InbandThinkingTracker()
    tracker.feed("reasoning</think>")
    assert tracker.has_visible_text is False
    tracker.feed("The answer is 42.")
    assert tracker.has_visible_text is True
