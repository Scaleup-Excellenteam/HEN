"""Autocomplete over a corpus of sentences, tolerating at most one typing error.

Public interface:

    get_best_k_completions(prefix: str) -> List[AutoCompleteData]

The design this package implements is described in
``docs/design/2026-08-31-autocomplete-design-review-v2.md``.
"""

from __future__ import annotations

from typing import List

from .config import Config, load_default_config
from .data import AutoCompleteData

__all__ = [
    "AutoCompleteData",
    "get_best_k_completions",
    "get_default_index",
    "reset_default_index",
    "__version__",
]

__version__ = "0.1.0"

_default_index = None


def get_default_index(config: Config | None = None):
    """Return the index the public function searches, preparing it if needed.

    The first call reads the corpus, or a cached index of it, which takes
    seconds on a large corpus; later calls reuse it. Pass a config to point at a
    different corpus, which replaces the one held.
    """
    global _default_index
    if _default_index is None or config is not None:
        from .cache import build_or_load

        _default_index = build_or_load(config or load_default_config())
    return _default_index


def reset_default_index() -> None:
    """Forget the prepared index, so the next call prepares it again."""
    global _default_index
    _default_index = None


# The signature below is fixed by the assignment and is written exactly as
# specified there, including `List[...]` rather than the modern `list[...]`.
def get_best_k_completions(prefix: str) -> List[AutoCompleteData]:
    """Return the best completions for the text typed so far, best first.

    Matches sentences that contain the text, and sentences that would contain it
    after one character is substituted, added or removed. Results are ordered by
    score, and equal scores alphabetically; each sentence appears at most once,
    with the best score any match of it earns. How many are returned is
    ``num_results`` in the configuration.

    The corpus is prepared on the first call. Use :func:`get_default_index`
    beforehand to control when that happens, or to search a different corpus.
    """
    from .engine import find_completions

    return find_completions(get_default_index(), prefix)
