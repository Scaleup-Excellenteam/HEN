"""The query sets each run is measured on.

Queries are drawn from the corpus being measured rather than written down, so a
run describes that corpus rather than the one this was written against. Drawing
is seeded, so two runs on the same corpus ask the same questions.

Every class is here for a reason. "typing" is what interactive use produces and
carries the percentile limits; the rest are adversarial, chosen because each
stresses a different part of the search: enormous suffix ranges, many duplicate
repairs, or every repair being looked up and none of them hitting.
"""

from __future__ import annotations

import random

import numpy as np

from autocomplete.index import SearchIndex
from autocomplete.normalize import ALPHABET

__all__ = ["CLASS_ORDER", "build"]

_QUERY_CHARACTERS = ALPHABET.decode("ascii")

#: Patterns that occupy the largest ranges in an English corpus.
_COMMON_PATTERNS = [" ", "e", "t", "the", "tion", "ation"]

_REPEATED = ["aaaa", "aaaaaa", "eeee", "thethethe", "abababab", "aaaaaaaaaa"]

_ABSENT_SHORT = ["zqx", "qqzz", "xqjz", "zzqqxx", "jqxz"]

#: The order classes are reported in: the representative one first, then the
#: adversarial ones roughly by how hard they are.
CLASS_ORDER = [
    "typing",
    "specific phrase",
    "short",
    "common patterns",
    "one typo",
    "repeated characters",
    "absent short",
    "long garbage",
]


def build(index: SearchIndex, seed: int = 20260831, scale: int = 1) -> dict[str, list[str]]:
    """Assemble every query class for one corpus.

    Args:
        index: The index the queries are drawn from.
        seed: Fixed so a run is repeatable.
        scale: Multiplies the size of the sampled classes.
    """
    rng = random.Random(seed)
    sampler = _Sampler(index, rng)

    return {
        "typing": _typing(sampler, rng, 12 * scale),
        "specific phrase": _phrases(sampler, rng, 20 * scale),
        "short": _short(),
        "common patterns": list(_COMMON_PATTERNS),
        "one typo": [
            _edit(sampler.substring(rng.choice([8, 12, 16, 20])), rng)
            for _ in range(20 * scale)
        ],
        "repeated characters": list(_REPEATED),
        "absent short": list(_ABSENT_SHORT),
        "long garbage": [
            "".join(rng.choices(_QUERY_CHARACTERS, k=length))
            for length in (20, 40, 60, 80, 100, 150, 200)
        ],
    }


def _typing(sampler: "_Sampler", rng: random.Random, sentences: int) -> list[str]:
    """A sentence typed from its beginning, in word-sized turns.

    This is what the interactive loop sends: every turn searches everything
    entered so far, so the queries grow. Starting at the beginning of a real
    sentence matters, because that is where typing starts, and such a prefix
    usually occurs in enough sentences to be answered from exact matches alone.

    Phrases taken from the middle of a sentence are a different regime, measured
    separately by :func:`_phrases`; mixing the two puts the median on the
    boundary between them, where it says nothing about either.
    """
    queries: list[str] = []
    for _ in range(sentences):
        text = sampler.sentence_start(rng.choice([18, 24, 30]))
        if not text:
            continue
        # Always finish on the whole text, so a corpus whose sentences are
        # shorter than one turn still produces queries rather than an empty
        # class whose gates would then go unjudged.
        stops = list(range(4, len(text) + 1, 4))
        if not stops or stops[-1] != len(text):
            stops.append(len(text))
        queries.extend(text[:end] for end in stops)
    return queries


def _phrases(sampler: "_Sampler", rng: random.Random, count: int) -> list[str]:
    """Longer runs taken from the middle of sentences.

    Specific enough that fewer than five sentences contain them, so the exact
    tier runs out and the remaining places are filled by walking the repair
    tiers. That makes this the everyday case that actually exercises the fuzzy
    search, as opposed to the adversarial classes below it.
    """
    return [sampler.substring(rng.choice([16, 20, 24, 28])) for _ in range(count)]


def _short() -> list[str]:
    letters = "etaoinshrq"
    digits = "013"
    pairs = ["th", "he", "in", "er", "an", "re", "on", "at"]
    return [*letters, *digits, *pairs]


def _edit(text: str, rng: random.Random) -> str:
    """Substitute, add or drop one character."""
    if not text:
        return "z"
    position = rng.randrange(len(text))
    character = rng.choice(_QUERY_CHARACTERS)
    operation = rng.choice(("substitute", "insert", "delete"))
    if operation == "substitute":
        return text[:position] + character + text[position + 1 :]
    if operation == "insert":
        return text[:position] + character + text[position:]
    return text[:position] + text[position + 1 :]


class _Sampler:
    """Draws real text out of an index."""

    def __init__(self, index: SearchIndex, rng: random.Random) -> None:
        self._text = bytes(index.records.norm_blob)
        self._starts = np.asarray(index.records.starts)
        self._lengths = np.diff(self._starts) - 1
        self._rng = rng

    def substring(self, length: int) -> str:
        """A run of ``length`` characters taken from inside one sentence."""
        record, length = self._pick(length)
        if record is None:
            return "a" * length
        start = int(self._starts[record])
        offset = self._rng.randrange(int(self._lengths[record]) - length + 1)
        return self._text[start + offset : start + offset + length].decode("ascii")

    def sentence_start(self, length: int) -> str:
        """The first ``length`` characters of a sentence."""
        record, length = self._pick(length)
        if record is None:
            return "a" * length
        start = int(self._starts[record])
        return self._text[start : start + length].decode("ascii")

    def _pick(self, length: int) -> tuple[int | None, int]:
        """A record long enough for ``length``, shortening it if none is."""
        length = max(1, min(length, int(self._lengths.max(initial=1))))
        candidates = (self._lengths >= length).nonzero()[0]
        if candidates.size == 0:
            return None, length
        return int(candidates[self._rng.randrange(candidates.size)]), length
