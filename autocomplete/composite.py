"""Searching more than one index and returning the true global best.

The corpus index is large, immutable and expensive to build. Documents a user
imports are few, change often, and are cheap to index on their own. Rather than
rebuilding one combined index every time somebody imports a file, each is
searched separately and the two answers are merged here.

Nothing in this module is specific to where a second index came from. It knows
about indexes and about the project's ordering policy, and about nothing else.

Why taking K from each index is enough
--------------------------------------

Write ``≺`` for the order :attr:`~autocomplete.data.AutoCompleteData.ranking_key`
induces: score descending, then the original sentence, source path and line
number ascending. Let the indexes hold disjoint record sets ``A`` and ``B``, and
let ``U = A ∪ B``.

1. **A record's score does not depend on which index holds it.** The engine
   scores a match by the repair that reached it, and a repair's score is a
   function of the query alone (:mod:`autocomplete.scoring` knows nothing of any
   index). So the score of record ``r`` is
   ``max{score(p) : p a repair of the query occurring in r}``, which mentions
   only the query and ``r``'s own text.

2. **``≺`` does not depend on which index holds the record either**: every one
   of its four components is a property of the record.

3. **``find_completions(I, q, K)`` returns exactly the ``≺``-least K records of
   ``I``.** That is the engine's own guarantee, and it is exact: a tier is never
   abandoned part-way, so a record is never passed over at a good score and then
   picked up later at a worse one.

4. **``topK(U) ∩ A ⊆ topK(A)``.** Take ``r ∈ topK(U) ∩ A``. Fewer than K members
   of ``U`` precede ``r`` under ``≺``. Since ``A ⊆ U``, fewer than K members of
   ``A`` precede ``r``, so ``r`` is among ``A``'s K smallest. The same argument
   gives ``topK(U) ∩ B ⊆ topK(B)``.

5. Therefore ``topK(U) ⊆ topK(A) ∪ topK(B)``. Merging the two K-element answers
   under ``≺`` and keeping the first K yields ``topK(U)`` exactly. Asking either
   index for more than K would be wasted work, and asking for fewer could drop a
   global winner, as when all K come from one index.

Ties, and why the result is still one definite list
---------------------------------------------------

Step 5 needs ``≺`` to be a *total* order, or "the first K" would not be
well defined. Within one index it is: two records sharing a score, a sentence
and a source path must differ in their line number. Across two indexes the four
fields could in principle coincide, so :func:`merge` breaks that last tie by the
order the answers were given to it, and the caller passes the base corpus first.
The result is one definite list, and the same list on every run.

Records are *not* deduplicated across indexes. That matches what the project
already does within one corpus, where two files holding the same sentence give
two results (see :func:`autocomplete.reference.find_best_k`), and it is what
keeps each result's own source and line number meaningful.

Settling a query without walking any repairs
--------------------------------------------

The engine already stops before considering a single repair when the query
itself occurs in K sentences, which is what makes typing cheap. Searching two
indexes separately loses that: a small overlay almost never has K matches of
its own, so it would walk the entire repair ladder on every keystroke while the
corpus was answering from the exact tier alone. Measured, that cost more than
the whole rest of the search.

So the exact tier is taken from both indexes first. If the two together supply K
matches, that is the answer, and no repair is enumerated anywhere:

    Every exact match scores ``2m`` for a query of m characters, and every
    repair scores strictly less. A substitution keeps ``m - 1`` characters and
    pays at least 1, an extra character keeps ``m - 1`` and pays at least 2, an
    omission keeps ``m`` and pays at least 2; so no repair can reach ``2m``.
    With K matches already at ``2m``, nothing scoring less can enter the top K,
    whichever index it is in.

    That K of them are the *right* K follows from the same subset argument as
    above, applied to the exact matches alone: the K best exact matches of the
    union lie within the K best of each index.

When the two together cannot supply K, repairs are needed and the full walk runs
on both indexes, exactly as before. The extra cost in that case is two suffix
lookups, which is nothing beside enumerating the repairs that follow.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .data import AutoCompleteData
from .engine import exact_completions, find_completions
from .index import SearchIndex

__all__ = ["merge", "search"]


def search(
    base: SearchIndex,
    overlay: SearchIndex | None,
    query: str | bytes,
    limit: int | None = None,
) -> list[AutoCompleteData]:
    """The best completions across a base index and an optional overlay.

    With no overlay this is ``find_completions`` and nothing else: the merge is
    not entered, no list is copied, and the cost is exactly what it is without
    the feature that supplies overlays.

    Args:
        base: The corpus index.
        overlay: A second index to search alongside it, or ``None``.
        query: What the user typed.
        limit: How many results to return, defaulting to what ``base`` was built
            to answer. Both indexes are asked for this many, which
            :func:`merge` needs to be able to return the true global best.

    Returns:
        Up to ``limit`` completions in the project's order, exactly as if every
        record of both indexes had been ranked together.
    """
    if overlay is None:
        return find_completions(base, query, limit)

    wanted = base.summary_width if limit is None else limit
    if wanted < 1:
        return []

    # The exact tier first, from both. When it fills, every result scores the
    # maximum a query of this length can earn and no repair could displace one,
    # so the answer is settled without enumerating any.
    settled = merge(
        [
            exact_completions(base, query, wanted),
            exact_completions(overlay, query, wanted),
        ],
        limit=wanted,
    )
    if len(settled) == wanted:
        return settled

    return merge(
        [
            find_completions(base, query, wanted),
            find_completions(overlay, query, wanted),
        ],
        limit=wanted,
    )


def merge(
    answers: Iterable[Sequence[AutoCompleteData]], *, limit: int
) -> list[AutoCompleteData]:
    """Combine per-index answers into the global best ``limit``.

    Each answer must already be that index's own best, in the project's order.
    Ordering is by :attr:`~autocomplete.data.AutoCompleteData.ranking_key`, and
    results that are identical under it keep the order of the answers they came
    from, so the caller decides that last tie by which index it lists first.
    """
    if limit < 1:
        return []
    # sorted() is stable, so chaining the answers in the caller's order and
    # sorting on the ranking key alone settles an exact tie in favour of the
    # earlier answer, without a synthetic field in the key to do it.
    combined = [result for answer in answers for result in answer]
    combined.sort(key=lambda result: result.ranking_key)
    return combined[:limit]
