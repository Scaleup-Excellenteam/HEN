"""A brute-force reference implementation, used to define correctness.

This module answers "what should the engine return?" in the most direct way
available: it looks at every source record, slides a window over the sentence,
and compares it against the query character by character. It is deliberately
slow and is never used to serve queries. Its purpose is to be obviously right,
so that the fast engine can be checked against it.

**Independence.** The production search path will find matches by enumerating
repaired forms of the query and looking them up in a suffix array. This module
must not share that reasoning, or a mistake in it would be invisible: a
differential test between two copies of the same idea proves nothing. So the
matching here is window alignment, and the penalty numbers are restated from the
assignment appendix rather than imported from :mod:`autocomplete.scoring`.
A test asserts the two tables still agree, which catches drift without creating
a shared cause of failure.

What is deliberately shared is everything that is not a search decision: the
canonical normalizer, the :class:`~autocomplete.data.AutoCompleteData` record,
and the result ordering policy. Those must be identical, not merely equivalent.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .data import AutoCompleteData
from .normalize import normalize
from .scoring import ALLOW_EMPTY_REPAIR

__all__ = [
    "SourceRecord",
    "best_alignment_score",
    "find_best_k",
    "load_records",
    "records_from_lines",
]

# Restated from the assignment appendix; positions are 1-based and position 5
# onwards shares the last value. Kept separate from autocomplete.scoring on
# purpose, see the module docstring.
_SUBSTITUTION_PENALTIES = {1: 5, 2: 4, 3: 3, 4: 2}
_SUBSTITUTION_TAIL = 1
_INDEL_PENALTIES = {1: 10, 2: 8, 3: 6, 4: 4}
_INDEL_TAIL = 2


def _substitution_penalty(position: int) -> int:
    return _SUBSTITUTION_PENALTIES.get(position, _SUBSTITUTION_TAIL)


def _indel_penalty(position: int) -> int:
    return _INDEL_PENALTIES.get(position, _INDEL_TAIL)


@dataclass(frozen=True)
class SourceRecord:
    """One corpus line, in both the form shown to the user and the form searched.

    Attributes:
        completed_sentence: The line exactly as it appears in the file.
        source_text: Path of the file relative to the corpus root, POSIX style.
        offset: 1-based line number within that file.
        normalized: :func:`~autocomplete.normalize.normalize` of the line.
    """

    completed_sentence: str
    source_text: str
    offset: int
    normalized: bytes

    @classmethod
    def from_line(
        cls, completed_sentence: str, source_text: str, offset: int
    ) -> "SourceRecord":
        """Build a record, normalizing the line with the canonical normalizer."""
        return cls(
            completed_sentence,
            source_text,
            offset,
            normalize(completed_sentence),
        )


def best_alignment_score(query: bytes, sentence: bytes) -> int | None:
    """Best score for a normalized query against a normalized sentence.

    Returns ``None`` when the sentence cannot be reached within one edit.

    Tries every alignment of the query against the sentence and keeps the
    highest score, because several occurrences or several different edits can
    explain the same match and the assignment asks for the best completion. The
    window lengths are the three an edit can produce: ``m`` when a character was
    typed wrongly, ``m - 1`` when one was typed too many, and ``m + 1`` when one
    was left out.
    """
    length = len(query)
    if length == 0:
        return None

    if query in sentence:
        # Nothing can beat an exact match: every repair either loses a matching
        # character or pays at least two points.
        return 2 * length

    best: int | None = None

    def offer(score: int) -> None:
        nonlocal best
        if best is None or score > best:
            best = score

    for start in range(len(sentence) + 1):
        # One character typed wrongly: same length, exactly one position differs.
        window = sentence[start : start + length]
        if len(window) == length:
            differing = [i for i in range(length) if query[i] != window[i]]
            if len(differing) == 1:
                offer(2 * (length - 1) - _substitution_penalty(differing[0] + 1))

        # One character too many: dropping it must reproduce the window.
        # A one-character query is skipped, since dropping its only character
        # leaves the empty string, which every sentence contains (decision D9,
        # shared with autocomplete.scoring so the two cannot disagree).
        if length >= 2 or ALLOW_EMPTY_REPAIR:
            window = sentence[start : start + length - 1]
            if len(window) == length - 1:
                for position in range(1, length + 1):
                    if query[: position - 1] + query[position:] == window:
                        offer(2 * (length - 1) - _indel_penalty(position))

        # One character left out: putting the window's character back must
        # reproduce it. Every typed character still matches, so all m count.
        window = sentence[start : start + length + 1]
        if len(window) == length + 1:
            for position in range(1, length + 2):
                restored = (
                    query[: position - 1]
                    + window[position - 1 : position]
                    + query[position - 1 :]
                )
                if restored == window:
                    offer(2 * length - _indel_penalty(position))

    return best


def find_best_k(
    query: str | bytes,
    records: Iterable[SourceRecord],
    k: int = 5,
) -> list[AutoCompleteData]:
    """Return the best ``k`` completions for ``query``, best first.

    Scores every record independently, so a record appears at most once, with
    the best score any alignment gives it. Records with identical text at
    different places stay separate results. Ordering is the project's policy:
    score descending, then the original sentence, source path and line number.

    An empty query, or one that normalizes away entirely, matches nothing.
    """
    if k < 0:
        raise ValueError(f"k must be >= 0, got {k}")

    normalized_query = normalize(query)
    if not normalized_query:
        return []

    matches: list[AutoCompleteData] = []
    for record in records:
        score = best_alignment_score(normalized_query, record.normalized)
        if score is not None:
            matches.append(
                AutoCompleteData(
                    record.completed_sentence,
                    record.source_text,
                    record.offset,
                    score,
                )
            )

    matches.sort(key=lambda item: item.ranking_key)
    return matches[:k]


def records_from_lines(
    lines_by_path: Mapping[str, Sequence[str]],
) -> list[SourceRecord]:
    """Build records from in-memory text, for tests that need no files.

    Line numbers are 1-based in the order given. Lines that normalize to nothing
    are skipped, matching what the index will store: they can never match.
    """
    records: list[SourceRecord] = []
    for source_text in sorted(lines_by_path):
        for offset, line in enumerate(lines_by_path[source_text], start=1):
            record = SourceRecord.from_line(line, source_text, offset)
            if record.normalized:
                records.append(record)
    return records


def load_records(root: Path | str) -> list[SourceRecord]:
    """Read every ``.txt`` file under ``root`` into records.

    A small deterministic reader for tests and for checking the reference against
    the real corpus. Milestone M3 replaces it with the cached, sorted record
    store the engine serves from; this one keeps no index and holds everything in
    Python objects.

    Directories and files are visited in sorted order, paths are reported
    relative to ``root``, line numbers are 1-based, and lines that normalize to
    nothing are skipped.
    """
    root = Path(root)
    records: list[SourceRecord] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in sorted(filenames):
            if not filename.endswith(".txt"):
                continue
            path = Path(dirpath) / filename
            source_text = path.relative_to(root).as_posix()
            data = path.read_bytes()
            for offset, raw_line in enumerate(data.split(b"\n"), start=1):
                # Trailing CR on CRLF files would otherwise show up in output.
                line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
                record = SourceRecord.from_line(line, source_text, offset)
                if record.normalized:
                    records.append(record)
    return records
