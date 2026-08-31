"""Answering queries from a built index.

Only exact matches are handled here so far. A query that is already a substring
of a sentence scores ``2 x length``, and no repair can beat that: every repair
either loses a matching character or pays a penalty of at least two. So exact
matches are the top of the result list whenever they exist, and finding them is
a single suffix-array lookup.

The fuzzy path, which repairs the query and walks the repairs in descending
score order, arrives in M5. Until then this is deliberately not wired to
:func:`autocomplete.get_best_k_completions`: with fewer than five exact matches
the public function would have to fill the remaining places with fuzzy ones, and
returning a short list instead would be wrong rather than merely incomplete.
"""

from __future__ import annotations

from .data import AutoCompleteData
from .index import SearchIndex
from .normalize import normalize
from .scoring import exact_score

__all__ = ["exact_completions"]


def exact_completions(
    index: SearchIndex,
    query: str | bytes,
    limit: int | None = None,
) -> list[AutoCompleteData]:
    """Return the best completions that contain the query exactly, best first.

    Every result scores the same, ``2 x`` the normalized query length, so the
    order among them is the tie-break order alone. Because records are stored in
    that order, this is the smallest record numbers in the matching range, which
    the block summaries answer without reading the range.

    Args:
        index: A built index.
        query: What the user typed. Normalized here, so case, punctuation and
            spacing do not have to match.
        limit: How many results to return, defaulting to the index's summary
            width. It cannot exceed that width, since the summaries were built
            to answer exactly that many.

    Returns:
        Up to ``limit`` completions. Fewer if the query occurs in fewer records,
        and none if it occurs nowhere, is empty once normalized, or is longer
        than the longest sentence.
    """
    wanted = index.summary_width if limit is None else limit
    if wanted < 1:
        return []
    if wanted > index.summary_width:
        raise ValueError(
            f"index was built to answer {index.summary_width} results at a time, "
            f"asked for {wanted}"
        )

    normalized = normalize(query)
    if not normalized:
        return []
    if len(normalized) > index.records.max_record_length:
        return []

    low, high = index.suffix.find(normalized)
    if high <= low:
        return []

    score = exact_score(len(normalized))
    return [
        index.records.completion(record, score)
        for record in index.blocks.smallest_record_ids(low, high, need=wanted)
    ]
