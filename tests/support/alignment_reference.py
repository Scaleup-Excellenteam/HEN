"""An independent scorer, written to cross-check the production one.

This implementation deliberately shares nothing with ``autocomplete.scoring``:
it slides a window over the sentence and compares it against the query directly,
rather than enumerating repaired patterns, and it restates the penalty numbers
from the assignment appendix instead of importing them. A mistake in the
production penalty table or in its repair generation therefore cannot be
mirrored here.

It is quadratic in the query length and linear in the sentence length, which is
fine for the tiny inputs used in tests.
"""

from __future__ import annotations

# Restated from the assignment appendix; positions are 1-based, 5 and beyond
# share the last value.
_SUBSTITUTION = {1: 5, 2: 4, 3: 3, 4: 2}
_INDEL = {1: 10, 2: 8, 3: 6, 4: 4}


def _substitution_penalty(position: int) -> int:
    return _SUBSTITUTION.get(position, 1)


def _indel_penalty(position: int) -> int:
    return _INDEL.get(position, 2)


def best_alignment_score(query: bytes, sentence: bytes) -> int | None:
    """Best score for ``query`` against ``sentence``, or None if they do not match.

    Both arguments must already be normalized. Where several edits could explain
    the same match, the best-scoring one wins, matching the assignment's request
    for the best completion.
    """
    length = len(query)
    if length == 0:
        return None

    if query in sentence:
        # No penalty can improve on an exact match: every alternative loses at
        # least one matching character or pays at least 2 points.
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
                position = differing[0] + 1
                offer(2 * (length - 1) - _substitution_penalty(position))

        # One character typed too many: dropping it must reproduce the window.
        if length >= 2:
            window = sentence[start : start + length - 1]
            if len(window) == length - 1:
                for position in range(1, length + 1):
                    if query[: position - 1] + query[position:] == window:
                        offer(2 * (length - 1) - _indel_penalty(position))

        # One character left out: adding the window's character must reproduce it.
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
