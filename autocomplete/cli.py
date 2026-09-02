"""The interactive completion loop.

The user types, presses Enter, and sees the best completions for everything
typed so far. Typing then continues from where it stopped, so a sentence is
built up across several turns rather than retyped each time, and each turn
searches the whole of it.

The accumulated text is echoed as the prompt for the next turn. That is what
makes continuing feel like continuing: the terminal shows what has been typed,
and whatever is typed next is appended to it.

Entering ``#`` finishes the sentence and starts over with nothing typed.
"""

from __future__ import annotations

import time
from typing import TextIO

from . import memory
from .data import AutoCompleteData
from .engine import find_completions
from .index import SearchIndex

__all__ = [
    "READY_MESSAGE",
    "RESET_CHARACTER",
    "format_stats",
    "format_suggestions",
    "run",
]

READY_MESSAGE = "The system is ready. Enter your text:"

#: TA-DECISION: the assignment says typing "#" means the sentence is finished,
#: without saying whether it has to be alone on the line. We reset on any line
#: containing it, and ignore whatever else that line held, on the grounds that
#: someone who types it has stopped thinking about this sentence. Restricting it
#: to a line equal to "#" is a change to _is_reset alone.
RESET_CHARACTER = "#"


def run(
    index: SearchIndex,
    stream_in: TextIO,
    stream_out: TextIO,
    limit: int | None = None,
    stats: bool = False,
) -> None:
    """Read queries and print completions until the input ends.

    Args:
        index: A prepared index.
        stream_in: Where queries are read from, a line at a time.
        stream_out: Where the prompt and results are written.
        limit: How many completions to show, defaulting to the number the index
            was built to answer.
        stats: Print how long each search took, and how much memory the process
            holds afterwards, under its results. Off by default, so that the
            session reads exactly as the assignment's worked example does.
    """
    _write(stream_out, f"{READY_MESSAGE}\n")

    # A terminal prints what is typed; a pipe does not. Standing in for it keeps
    # a scripted run readable, and reading like the session it represents,
    # instead of running the entry and the results together on one line.
    echo_input = not _is_terminal(stream_in)

    typed_so_far = ""
    while True:
        # Echoing what has been typed makes the next line continue it.
        _write(stream_out, typed_so_far)
        line = stream_in.readline()
        if not line:
            _write(stream_out, "\n")
            return

        # Strip the carriage return as well, so input from a Windows terminal
        # does not leave one inside the echoed prompt.
        entry = line.rstrip("\r\n")
        if echo_input:
            _write(stream_out, f"{entry}\n")

        if _is_reset(entry):
            typed_so_far = ""
            _write(stream_out, f"\n{READY_MESSAGE}\n")
            continue

        typed_so_far += entry
        if not typed_so_far.strip():
            continue

        # Timed around the search alone: the time to render and write the
        # results is the terminal's, not the engine's.
        started = time.perf_counter()
        results = find_completions(index, typed_so_far, limit)
        elapsed = time.perf_counter() - started

        _write(stream_out, format_suggestions(results))
        if stats:
            _write(stream_out, format_stats(elapsed))


def format_suggestions(results: list[AutoCompleteData]) -> str:
    """Render completions the way the assignment's worked example shows them."""
    if not results:
        return "No suggestions found.\n"

    heading = (
        "Here is 1 suggestion:"
        if len(results) == 1
        else f"Here are {len(results)} suggestions:"
    )
    lines = [f"{position}. {result}" for position, result in enumerate(results, 1)]
    return "\n".join([heading, *lines]) + "\n"


def format_stats(seconds: float) -> str:
    """Render the timing and memory line printed under a result set.

    Memory is read after the search, so it reflects the pages that search made
    resident: with a memory-mapped index the figure climbs over the first few
    queries and then settles.
    """
    line = f"   found in {seconds * 1000:.1f} ms"
    resident = memory.resident_bytes()
    if resident is not None:
        line += f" | memory {memory.format_gb(resident)}"
    return f"{line}\n"


def _is_reset(entry: str) -> bool:
    return RESET_CHARACTER in entry


def _is_terminal(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _write(stream: TextIO, text: str) -> None:
    stream.write(text)
    stream.flush()
