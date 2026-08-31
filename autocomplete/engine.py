"""Answering queries from a built index.

A query matches a sentence when it is a substring of it, or becomes one after a
single character edit. Rather than testing sentences, the engine enumerates the
strings the query could be repaired into, which M1 does, and looks each one up
in the suffix array. A repair's score depends only on the repair, never on which
sentence it matches, so the repairs can be grouped into tiers of equal score and
walked from the best score down.

The walk
--------

Tier zero is the query itself, scoring twice its length. Nothing can beat that:
a repair either loses a matching character or pays a penalty of at least two. So
if the query occurs in K sentences the answer is settled without considering a
single repair.

Otherwise each tier is processed in full before any of its winners are chosen.
Every pattern in the tier contributes candidates, they are merged, and the
smallest record numbers are taken, which is the tie-break order because the
record store is laid out that way. Records already chosen from a higher tier are
excluded, so a sentence reachable several ways keeps its best score and appears
once.

The walk stops as soon as K results are held: every remaining tier scores
strictly lower, so nothing in them could displace a result already chosen. It
never stops part-way through a tier, since the patterns of one tier are equally
good and choosing among them needs all of them.

Why a bounded number of candidates per pattern is enough
--------------------------------------------------------

Each pattern is asked for only ``need`` record numbers, where ``need`` is how
many results are still missing, rather than everything its range contains. That
is sufficient. Let x be one of the ``need`` smallest record numbers of the
union of the tier's ranges, after removing the already-chosen ones, and let p be
a pattern whose range contains x. Any record number in p's range that is smaller
than x and not already chosen is itself in that answer and comes before x, so
there are at most ``need - 1`` of them. So x is among the ``need`` smallest of
p's range, and asking p for that many cannot miss it.

This also keeps the block summaries inside the invariant they were built for:
``need`` plus the number excluded is exactly K, which never exceeds the width
the summaries were built with.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from .data import AutoCompleteData
from .index import SearchIndex
from .normalize import normalize
from .scoring import (
    EditType,
    Repair,
    ScoreTier,
    best_scores_by_pattern,
    exact_score,
    group_into_tiers,
    iter_repairs,
)

__all__ = ["exact_completions", "find_completions"]


def find_completions(
    index: SearchIndex,
    query: str | bytes,
    limit: int | None = None,
) -> list[AutoCompleteData]:
    """Return the best completions for ``query``, best first.

    Args:
        index: A built index.
        query: What the user typed. Normalized here, so case, punctuation and
            spacing need not match.
        limit: How many results to return, defaulting to the index's summary
            width. It cannot exceed that width, which is the number of results
            the block summaries were built to answer exactly.

    Returns:
        Up to ``limit`` completions, ordered by score and then by the project's
        tie-break rule. Each sentence appears at most once, with the best score
        any match of it earns. Empty when the query normalizes away, or when
        even the shortest repair of it is longer than the longest sentence.
    """
    wanted = _requested(index, limit)
    if wanted < 1:
        return []

    normalized = normalize(query)
    if not normalized:
        return []

    longest = index.records.max_record_length
    # The shortest repair drops one character, so a query longer than that
    # cannot match through any path.
    if len(normalized) - 1 > longest:
        return []

    chosen: set[int] = set()
    selected: list[tuple[int, int]] = []

    if len(normalized) <= longest:
        for record in _records_for(index, normalized, wanted, chosen):
            selected.append((record, exact_score(len(normalized))))
            chosen.add(record)
        if len(selected) == wanted:
            return _completions(index, selected)

    for tier in _fuzzy_tiers(normalized):
        need = wanted - len(selected)
        candidates: set[int] = set()
        for pattern in tier.patterns:
            candidates.update(_records_for(index, pattern, need, chosen))
        for record in sorted(candidates)[:need]:
            selected.append((record, tier.score))
            chosen.add(record)
        if len(selected) == wanted:
            break

    return _completions(index, selected)


def exact_completions(
    index: SearchIndex,
    query: str | bytes,
    limit: int | None = None,
) -> list[AutoCompleteData]:
    """Return only the completions that contain the query exactly.

    The first tier of :func:`find_completions`, exposed on its own for tests and
    for callers that want matches without any correction.
    """
    wanted = _requested(index, limit)
    if wanted < 1:
        return []

    normalized = normalize(query)
    if not normalized or len(normalized) > index.records.max_record_length:
        return []

    score = exact_score(len(normalized))
    return _completions(
        index,
        [(record, score) for record in _records_for(index, normalized, wanted, ())],
    )


def _requested(index: SearchIndex, limit: int | None) -> int:
    """How many results to look for, refusing more than the index can prove."""
    wanted = index.summary_width if limit is None else limit
    if wanted > index.summary_width:
        raise ValueError(
            f"index was built to answer {index.summary_width} results at a time, "
            f"asked for {wanted}"
        )
    return wanted


def _records_for(
    index: SearchIndex,
    pattern: bytes,
    need: int,
    exclude: Collection[int],
) -> list[int]:
    """The ``need`` smallest records holding ``pattern``, skipping ``exclude``.

    Whatever the size of the range, this reads the block summaries rather than
    the range itself, so a pattern occurring millions of times costs no more
    than one occurring twice.
    """
    if not pattern or need < 1:
        return []
    low, high = index.suffix.find(pattern)
    if high <= low:
        return []
    return index.blocks.smallest_record_ids(low, high, need=need, exclude=exclude)


def _fuzzy_tiers(query: bytes) -> list[ScoreTier]:
    """Repairs of the query, grouped by score, best first.

    Insertions at either end of the query are dropped: they cannot produce a
    result. If ``c + query`` occurs in a sentence then so does ``query`` itself,
    as part of that same text, so the sentence is an exact match and was already
    offered by tier zero at a strictly higher score. The same holds for
    ``query + c``.

    The dropping happens before patterns are deduplicated, which matters: a
    pattern can be reachable both by an end insertion and by one in the middle,
    as ``ab`` reaches ``aab`` by inserting at either position 1 or 2. Filtering
    afterwards would discard the interior repair's better score along with it.
    """
    return group_into_tiers(
        best_scores_by_pattern(
            repair
            for repair in iter_repairs(query)
            if not _inserts_at_an_end(repair, len(query))
        )
    )


def _inserts_at_an_end(repair: Repair, query_length: int) -> bool:
    return repair.edit_type is EditType.INSERTION and repair.position in (
        1,
        query_length + 1,
    )


def _completions(
    index: SearchIndex, selected: Iterable[tuple[int, int]]
) -> list[AutoCompleteData]:
    return [index.records.completion(record, score) for record, score in selected]
