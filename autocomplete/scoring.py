"""Match scoring, repair generation and score tiers.

The assignment scores a match as ``2 x (matching characters)`` minus a penalty
for the single edit, if any. The penalty depends on the edit type and on the
position of the edited character, counted from the start of the normalized query.

The key structural fact, and the reason the engine is organised the way it is:
**a repair's score does not depend on which sentence it matches**. So instead of
scoring candidate sentences, we enumerate every string the query could be
repaired into, label each with its score, and look those strings up in the index.
Anything found is a match whose score is already known.

This module is deliberately free of any index or corpus knowledge.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum

from .normalize import ALPHABET

__all__ = [
    "ALLOW_EMPTY_REPAIR",
    "EditType",
    "Repair",
    "ScoreTier",
    "best_scores_by_pattern",
    "deletion_score",
    "exact_score",
    "group_into_tiers",
    "indel_penalty",
    "insertion_score",
    "iter_repairs",
    "repair_tiers",
    "substitution_penalty",
    "substitution_score",
]

# Penalties for edit positions 1..4; position 5 and beyond use the tail value.
_SUBSTITUTION_PENALTIES = (5, 4, 3, 2)
_SUBSTITUTION_TAIL = 1
_INDEL_PENALTIES = (10, 8, 6, 4)
_INDEL_TAIL = 2

#: TA-DECISION D9: repairing a one-character query by deleting its only
#: character yields the empty pattern, which is a substring of every sentence and
#: would make everything match at a fixed score. We exclude it. Other repairs
#: that happen to score negatively stay legal and simply rank last.
ALLOW_EMPTY_REPAIR = False


class EditType(Enum):
    """The repair applied to the query to turn it into a substring."""

    SUBSTITUTION = "substitution"
    #: The user typed one character too many; the repair deletes it.
    DELETION = "deletion"
    #: The user left one character out; the repair inserts it.
    INSERTION = "insertion"


@dataclass(frozen=True)
class Repair:
    """One repaired form of a query, with the score any match of it earns."""

    pattern: bytes
    score: int
    edit_type: EditType
    #: 1-based. For substitutions and deletions this is a position in the query;
    #: for insertions it is the position the new character occupies in
    #: ``pattern``.
    position: int


@dataclass(frozen=True)
class ScoreTier:
    """All repaired patterns that earn one particular score."""

    score: int
    patterns: tuple[bytes, ...]


def substitution_penalty(position: int) -> int:
    """Penalty for substituting the character at 1-based ``position``."""
    _check_position(position)
    if position <= len(_SUBSTITUTION_PENALTIES):
        return _SUBSTITUTION_PENALTIES[position - 1]
    return _SUBSTITUTION_TAIL


def indel_penalty(position: int) -> int:
    """Penalty for an extra or a missing character at 1-based ``position``."""
    _check_position(position)
    if position <= len(_INDEL_PENALTIES):
        return _INDEL_PENALTIES[position - 1]
    return _INDEL_TAIL


def exact_score(query_length: int) -> int:
    """Score when the query is already a substring: every character matches."""
    if query_length < 0:
        raise ValueError(f"query_length must be >= 0, got {query_length}")
    return 2 * query_length


def substitution_score(query_length: int, position: int) -> int:
    """Score when one typed character must be replaced.

    The substituted character earns no matching points, leaving
    ``query_length - 1`` matches.
    """
    _check_within_query(query_length, position)
    return 2 * (query_length - 1) - substitution_penalty(position)


def deletion_score(query_length: int, position: int) -> int:
    """Score when the user typed one character too many and it is deleted.

    ``position`` is where the extra character sits in the query. It earns no
    matching points, leaving ``query_length - 1`` matches. The appendix example
    is ``"or knot"`` -> ``"or not"``: ``2 x 6 - 4 = 8``.
    """
    _check_within_query(query_length, position)
    return 2 * (query_length - 1) - indel_penalty(position)


def insertion_score(query_length: int, position: int) -> int:
    """Score when the user left a character out and it is inserted.

    Every character the user typed matches, so all ``query_length`` of them
    count; the inserted character earns nothing. ``position`` is where the new
    character lands in the repaired string, so it ranges over
    ``1..query_length + 1``. The appendix example is ``"or nt"`` -> ``"or not"``:
    ``2 x 5 - 2 = 8``.
    """
    if query_length < 1:
        raise ValueError(f"query_length must be >= 1, got {query_length}")
    if not 1 <= position <= query_length + 1:
        raise ValueError(
            f"insertion position must be in 1..{query_length + 1}, got {position}"
        )
    return 2 * query_length - indel_penalty(position)


def iter_repairs(query: bytes, alphabet: bytes = ALPHABET) -> Iterator[Repair]:
    """Yield every legal one-edit repair of ``query``.

    Patterns repeat: different positions, and different edit types, can produce
    the same string. Callers reduce them with :func:`best_scores_by_pattern`.
    The unrepaired query is not yielded; an exact match is scored separately with
    :func:`exact_score` and always outranks every repair.

    Yields ``len(query) * (len(alphabet) - 1)`` substitutions,
    ``len(query)`` deletions (none for a one-character query, see
    :data:`ALLOW_EMPTY_REPAIR`) and ``(len(query) + 1) * len(alphabet)``
    insertions.
    """
    length = len(query)
    if length == 0:
        return

    for position in range(1, length + 1):
        score = substitution_score(length, position)
        replaced = query[position - 1]
        prefix, suffix = query[: position - 1], query[position:]
        for char in alphabet:
            if char != replaced:
                yield Repair(
                    prefix + bytes([char]) + suffix,
                    score,
                    EditType.SUBSTITUTION,
                    position,
                )

    if length >= 2 or ALLOW_EMPTY_REPAIR:
        for position in range(1, length + 1):
            yield Repair(
                query[: position - 1] + query[position:],
                deletion_score(length, position),
                EditType.DELETION,
                position,
            )

    for position in range(1, length + 2):
        score = insertion_score(length, position)
        prefix, suffix = query[: position - 1], query[position - 1 :]
        for char in alphabet:
            yield Repair(
                prefix + bytes([char]) + suffix,
                score,
                EditType.INSERTION,
                position,
            )


def best_scores_by_pattern(repairs: Iterable[Repair]) -> dict[bytes, int]:
    """Collapse repairs to ``pattern -> best achievable score``.

    A pattern reachable several ways must keep its **highest** score: those
    repairs are alternative explanations of the same match, and the assignment
    asks for the best one. For ``b"aab"``, deleting either of the first two
    characters gives ``b"ab"``, but deleting at position 2 costs 8 rather than
    10, so ``b"ab"`` keeps the higher score.

    Taking an iterable rather than a query lets the engine drop repairs it has
    proved cannot match before they are reduced.
    """
    best: dict[bytes, int] = {}
    for repair in repairs:
        current = best.get(repair.pattern)
        if current is None or repair.score > current:
            best[repair.pattern] = repair.score
    return best


def group_into_tiers(pattern_scores: Mapping[bytes, int]) -> list[ScoreTier]:
    """Group patterns by score, highest score first.

    Patterns are sorted inside each tier so results are reproducible. The engine
    walks tiers in order and may stop only at a tier boundary: within a tier all
    patterns are equally good, so leaving one out could hide a result that
    alphabetical tie-breaking would have ranked first.
    """
    grouped: dict[int, list[bytes]] = {}
    for pattern, score in pattern_scores.items():
        grouped.setdefault(score, []).append(pattern)
    return [
        ScoreTier(score, tuple(sorted(grouped[score])))
        for score in sorted(grouped, reverse=True)
    ]


def repair_tiers(query: bytes, alphabet: bytes = ALPHABET) -> list[ScoreTier]:
    """All one-edit repairs of ``query`` as score tiers, best score first."""
    return group_into_tiers(best_scores_by_pattern(iter_repairs(query, alphabet)))


def _check_position(position: int) -> None:
    if position < 1:
        raise ValueError(f"position is 1-based, got {position}")


def _check_within_query(query_length: int, position: int) -> None:
    if query_length < 1:
        raise ValueError(f"query_length must be >= 1, got {query_length}")
    if not 1 <= position <= query_length:
        raise ValueError(
            f"position must be in 1..{query_length}, got {position}"
        )
