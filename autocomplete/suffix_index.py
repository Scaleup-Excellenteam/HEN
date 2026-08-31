"""The suffix array over the normalized corpus, and exact substring lookup.

A suffix array lists every position in the text, ordered by the text that starts
there. Because that order is sorted, all the places a pattern occurs form one
contiguous run of entries, found by two binary searches. The array holds
positions, never the suffixes themselves, so it costs four bytes per character
of corpus rather than storing anything quadratic.

Searching the normalized blob means a match can never cross from one sentence
into the next: records are joined with a separator that normalization can never
produce, so no normalized pattern contains one, and therefore no occurrence of a
pattern can span the boundary. That property is what makes a hit in this index
directly usable as a match against exactly one record.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .normalize import ALPHABET
from .records import RECORD_SEPARATOR, Buffer

__all__ = [
    "SUFFIX_ARRAY_FILE",
    "SuffixIndex",
    "SuffixIndexError",
    "verify_builder",
]

SUFFIX_ARRAY_FILE = "suffix_array.npy"

# Upper bound key for a prefix range: any pattern followed by this byte sorts
# after every string that starts with that pattern. This works only because the
# byte is larger than anything the text can hold, which _check_sentinel proves
# from the alphabet and separator rather than assuming.
_UPPER_KEY = b"\xff"

_INSTALL_HINT = (
    "install it with: pip install -r requirements.txt "
    "(or: pip install 'pydivsufsort>=0.0.14,<0.1')"
)


class SuffixIndexError(RuntimeError):
    """Raised when the index cannot be built or read back."""


def _check_sentinel() -> None:
    """The upper bound key must exceed every byte the text can contain."""
    highest = max(max(ALPHABET), max(RECORD_SEPARATOR))
    if highest >= _UPPER_KEY[0]:
        raise SuffixIndexError(
            f"the prefix upper bound {_UPPER_KEY!r} is not above every text byte "
            f"(highest is 0x{highest:02x})"
        )


_check_sentinel()


def verify_builder() -> None:
    """Check that the suffix array builder is present and produces sorted output.

    Called before a build so that a missing or broken dependency is reported as
    an actionable message rather than as a failure part-way through indexing a
    large corpus.

    Raises:
        SuffixIndexError: if the builder is unavailable or disagrees with a
            directly computed suffix order.
    """
    try:
        from pydivsufsort import divsufsort
    except ImportError as exc:
        raise SuffixIndexError(
            f"building the search index needs the pydivsufsort package; {_INSTALL_HINT}"
        ) from exc

    sample = b"banana\nband\n"
    expected = sorted(range(len(sample)), key=lambda start: sample[start:])
    try:
        produced = divsufsort(sample)
    except Exception as exc:  # a broken or incompatible build of the extension
        raise SuffixIndexError(
            f"pydivsufsort is installed but failed on a self-test: {exc}. {_INSTALL_HINT}"
        ) from exc

    if list(produced) != expected:
        raise SuffixIndexError(
            "pydivsufsort returned an unexpected suffix order on a self-test, so "
            "the installed build cannot be trusted to index the corpus"
        )


@dataclass(frozen=True)
class SuffixIndex:
    """Exact substring search over a text.

    Attributes:
        text: The normalized blob being indexed. Borrowed from the record store
            rather than copied, so the two always describe the same bytes.
        positions: Start offsets into ``text``, ordered by the text at each one.
        max_pattern_length: Length of the longest record. A longer pattern
            cannot occur, since occurrences never cross a separator, so searches
            for one stop immediately.
    """

    text: Buffer
    positions: np.ndarray
    max_pattern_length: int

    def __len__(self) -> int:
        return int(self.positions.shape[0])

    @classmethod
    def build(cls, text: Buffer, max_pattern_length: int) -> "SuffixIndex":
        """Construct the suffix array of ``text``."""
        verify_builder()
        from pydivsufsort import divsufsort

        data = bytes(text)
        if not data:
            return cls(text, np.empty(0, dtype=np.int32), max_pattern_length)

        if len(data) > np.iinfo(np.int32).max:
            raise SuffixIndexError(
                f"text is {len(data)} bytes, too large for 32-bit suffix positions"
            )

        positions = divsufsort(data)
        positions = np.ascontiguousarray(positions, dtype=np.int32)
        if positions.ndim != 1 or positions.shape[0] != len(data):
            raise SuffixIndexError(
                f"suffix array has shape {positions.shape}, expected ({len(data)},)"
            )
        return cls(text, positions, max_pattern_length)

    def find(self, pattern: bytes) -> tuple[int, int]:
        """Return the half-open range of entries whose suffix starts with ``pattern``.

        The range is empty, ``(x, x)``, when the pattern does not occur. Every
        entry inside it is one occurrence, so ``hi - lo`` is the number of times
        the pattern appears in the text.

        Args:
            pattern: Normalized text to look for. Callers are expected to pass
                the output of :func:`~autocomplete.normalize.normalize`, so the
                pattern holds only alphabet bytes.

        Raises:
            ValueError: for an empty pattern, which every position would match,
                or for one containing a record separator, which normalization
                cannot produce and which no occurrence could contain.
        """
        if not pattern:
            raise ValueError("cannot search for an empty pattern")
        if RECORD_SEPARATOR in pattern:
            raise ValueError("a search pattern cannot contain the record separator")
        if len(pattern) > self.max_pattern_length:
            # No record is this long and matches never cross a separator.
            return 0, 0

        low = self._lower_bound(pattern)
        if low == len(self):
            return low, low
        return low, self._lower_bound(pattern + _UPPER_KEY)

    def occurrences(self, low: int, high: int) -> np.ndarray:
        """The text positions in a range returned by :meth:`find`."""
        return self.positions[low:high]

    def _lower_bound(self, key: bytes) -> int:
        """First entry whose suffix is not less than ``key``."""
        text = self.text
        positions = self.positions
        width = len(key)
        low, high = 0, int(positions.shape[0])
        while low < high:
            middle = (low + high) // 2
            start = int(positions[middle])
            # Comparing a slice rather than the whole suffix keeps each step to
            # the pattern length; bytes compare lexicographically, and a suffix
            # shorter than the key compares less, which is the order wanted.
            if text[start : start + width] < key:
                low = middle + 1
            else:
                high = middle
        return low

    def write_to(self, directory: Path | str) -> None:
        """Write the suffix array into ``directory``."""
        np.save(Path(directory) / SUFFIX_ARRAY_FILE, self.positions, allow_pickle=False)

    @classmethod
    def read_from(
        cls,
        directory: Path | str,
        text: Buffer,
        max_pattern_length: int,
        use_mmap: bool = True,
    ) -> "SuffixIndex":
        """Read a suffix array written by :meth:`write_to`.

        The text is supplied by the caller rather than stored again, so the index
        and the record store cannot describe different bytes.
        """
        positions = np.load(
            Path(directory) / SUFFIX_ARRAY_FILE,
            mmap_mode="r" if use_mmap else None,
            allow_pickle=False,
        )
        index = cls(text, positions, max_pattern_length)
        index.check_structure()
        return index

    def check_structure(self) -> None:
        """Check the array can index the text it was loaded beside."""
        if self.positions.ndim != 1:
            raise SuffixIndexError(
                f"suffix array must be one-dimensional, got {self.positions.ndim} dimensions"
            )
        if not np.issubdtype(self.positions.dtype, np.integer):
            raise SuffixIndexError(
                f"suffix array must hold integers, got {self.positions.dtype}"
            )
        if len(self) != len(self.text):
            raise SuffixIndexError(
                f"suffix array holds {len(self)} positions but the text is "
                f"{len(self.text)} bytes"
            )
