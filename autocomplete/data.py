"""The result record returned by the completion function.

The dataclass field names and types are fixed by the assignment and must not be
renamed or reordered.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AutoCompleteData", "tie_break_key"]


def tie_break_key(
    completed_sentence: str, source_text: str, offset: int
) -> tuple[str, str, int]:
    """Order completions that share a score.

    TA-DECISION D7': the assignment orders equal-scoring "strings" alphabetically
    without saying which form of the string it means. We order by the *original*
    sentence as returned to the user, ascending by codepoint, because that is the
    string the user sees and what a grader gets from a plain ``sorted()``.
    ``(source_text, offset)`` breaks the remaining ties so the total order is
    deterministic even for byte-identical lines in different files.

    Switching interpretation (normalized text, or case-insensitive original) is a
    change to this function alone; the record store sorts with the same key, so
    the engine needs no change. Note that for valid UTF-8, bytewise order equals
    codepoint order, which is what lets the record store apply this ordering to
    encoded bytes.
    """
    return (completed_sentence, source_text, offset)


@dataclass
class AutoCompleteData:
    """One completion suggestion.

    Attributes:
        completed_sentence: The matching line in its original form, punctuation
            and capitalisation included.
        source_text: Path of the file the line came from, relative to the corpus
            root (TA-DECISION: relative path rather than basename).
        offset: 1-based line number of the sentence within that file.
        score: Match score per the assignment's scoring table.
    """

    completed_sentence: str
    source_text: str
    offset: int
    score: int

    def __str__(self) -> str:
        """Render one suggestion in the format shown in the assignment example."""
        return (
            f"{self.completed_sentence} "
            f"({self.source_text}:{self.offset}, score={self.score})"
        )

    @property
    def ranking_key(self) -> tuple[int, str, str, int]:
        """Sort key implementing the full result order: score descending, then
        :func:`tie_break_key` ascending."""
        return (
            -self.score,
            *tie_break_key(self.completed_sentence, self.source_text, self.offset),
        )
