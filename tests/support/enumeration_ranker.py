"""A second full ranker, built the way the production engine will think.

This exists only to be compared against :mod:`autocomplete.reference`. It
answers the same question by the opposite route: instead of aligning the query
against each sentence, it enumerates every repaired form of the query with the
M1 primitives and asks which sentences contain one.

That is the reasoning the real engine will use, with the suffix array replaced
by a plain substring check. So agreement between this and the reference tests
the whole result contract, matching, score maximization, deduplication and
ordering, before any index exists. It is test code and is never imported by the
package.
"""

from __future__ import annotations

from collections.abc import Iterable

from autocomplete.data import AutoCompleteData
from autocomplete.normalize import normalize
from autocomplete.reference import SourceRecord
from autocomplete.scoring import best_scores_by_pattern, exact_score, iter_repairs


def rank(
    query: str | bytes,
    records: Iterable[SourceRecord],
    k: int = 5,
) -> list[AutoCompleteData]:
    """Return the best ``k`` completions, ranked like the reference engine."""
    normalized_query = normalize(query)
    if not normalized_query:
        return []

    # The repaired patterns depend only on the query, so they are built once.
    pattern_scores = best_scores_by_pattern(iter_repairs(normalized_query))
    exact = exact_score(len(normalized_query))

    matches: list[AutoCompleteData] = []
    for record in records:
        sentence = record.normalized
        if normalized_query in sentence:
            score: int | None = exact
        else:
            score = None
            for pattern, pattern_score in pattern_scores.items():
                if (score is None or pattern_score > score) and pattern in sentence:
                    score = pattern_score
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
