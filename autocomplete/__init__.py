"""Autocomplete over a corpus of sentences, tolerating at most one typing error.

Public interface:

    get_best_k_completions(prefix: str) -> List[AutoCompleteData]

The design this package implements is described in
``docs/design/2026-08-31-autocomplete-design-review-v2.md``.
"""

from __future__ import annotations

from typing import List

from .data import AutoCompleteData

__all__ = ["AutoCompleteData", "get_best_k_completions", "__version__"]

__version__ = "0.1.0"


# The signature below is fixed by the assignment and is written exactly as
# specified there, including `List[...]` rather than the modern `list[...]`.
def get_best_k_completions(prefix: str) -> List[AutoCompleteData]:
    """Return the best completions for the text typed so far, best first.

    Not implemented yet: the search engine arrives in milestone M5. Milestones
    M0-M1 provide normalization, scoring and repair generation, which this
    function will build on.
    """
    raise NotImplementedError(
        "the completion engine is implemented in milestone M5; "
        "normalization, scoring and repair generation are available now via "
        "autocomplete.normalize and autocomplete.scoring"
    )
