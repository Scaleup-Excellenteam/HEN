"""Picking the best records out of a suffix-array range.

A common pattern occupies an enormous range: a single space occurs thirteen
million times in the real corpus. Every one of those occurrences scores the
same, so the answer is decided entirely by the tie-break order, and because
records are stored in that order the answer is simply the smallest record
numbers in the range. Reading thirteen million entries to learn that takes
hundreds of milliseconds, which the design review measured and rejected.

The fix is to precompute, for each fixed-size block of the suffix array, the K
smallest distinct record numbers that block covers. A range then reduces to the
summaries of the whole blocks it contains plus the two partial blocks at its
ends, which is at most a few thousand entries however large the range is.

**Why the summaries are enough.** Let the parts be the blocks and boundary
fragments a range decomposes into, and suppose we want the ``need`` smallest
distinct record numbers of the union, after removing an excluded set, where
``need + len(excluded) <= K``. Take any x in that answer and look at the
distinct record numbers smaller than x inside x's own part. Each is either
excluded, and there are at most ``len(excluded)`` of those, or it is in the
union and not excluded and smaller than x, so it is itself part of the answer
and comes before x: at most ``need - 1`` of those. So fewer than
``len(excluded) + need <= K`` distinct record numbers precede x within its part,
which means x is among that part's K smallest. Keeping K per block therefore
loses nothing. The boundary fragments are not summarized at all, they are read
in full, so they contribute their complete set.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .progress import NULL_SINK, BuildPhase, ProgressSink

__all__ = [
    "BLOCK_SUMMARY_FILE",
    "DEFAULT_BLOCK_SIZE",
    "SENTINEL",
    "BlockSummaries",
    "BlockSummaryError",
]

BLOCK_SUMMARY_FILE = "block_summaries.npy"

#: Suffix-array entries per block. Large enough that summaries stay small (a
#: few hundred kilobytes for the real corpus) and small enough that the two
#: partial blocks at a range's ends stay cheap to read exactly.
DEFAULT_BLOCK_SIZE = 4096

#: Fills summary slots for blocks covering fewer than K distinct records. Chosen
#: above every possible record number so it sorts last and is easy to drop.
SENTINEL = np.iinfo(np.int32).max


class BlockSummaryError(RuntimeError):
    """Raised when summaries cannot be built or do not match their index."""


@dataclass(frozen=True)
class BlockSummaries:
    """Per-block shortlists of the smallest record numbers.

    Attributes:
        summaries: One row per block, holding that block's K smallest distinct
            record numbers in ascending order, padded with :data:`SENTINEL`.
        positions: The suffix array the blocks describe.
        starts: Record start offsets, for mapping a boundary entry to its record.
        block_size: Suffix-array entries covered by one row.
        width: K, the number of record numbers kept per block.
    """

    summaries: np.ndarray
    positions: np.ndarray
    starts: np.ndarray
    block_size: int
    width: int

    @classmethod
    def build(
        cls,
        positions: np.ndarray,
        starts: np.ndarray,
        *,
        width: int,
        block_size: int = DEFAULT_BLOCK_SIZE,
        sink: ProgressSink | None = None,
    ) -> "BlockSummaries":
        """Summarize a suffix array block by block.

        The record number of every suffix is computed once, in one vectorized
        pass, and released as soon as the summaries are built: keeping it would
        cost four bytes per corpus character, which the design review measured as
        not worth its 395 MB against summaries that answer the same question.
        """
        if width < 1:
            raise BlockSummaryError(f"summary width must be at least 1, got {width}")
        if block_size < 1:
            raise BlockSummaryError(f"block size must be at least 1, got {block_size}")
        record_count = int(starts.shape[0]) - 1
        if record_count >= SENTINEL:
            raise BlockSummaryError(
                f"{record_count} records does not fit below the sentinel value"
            )

        total = int(positions.shape[0])
        block_count = -(-total // block_size)
        summaries = np.full((block_count, width), SENTINEL, dtype=np.int32)

        # The one loop in the whole build that knows how much is left to do, so
        # this is the one phase with a genuine within-phase total.
        watcher = sink or NULL_SINK
        watcher.begin(
            BuildPhase.BUILDING_BLOCK_SUMMARIES,
            detail=f"Summarizing {block_count:,} blocks of {block_size:,} entries.",
            total=block_count,
        )

        if total:
            # One searchsorted over the whole array, rather than per query.
            record_of_suffix = (
                np.searchsorted(starts, positions, side="right") - 1
            ).astype(np.int32)
            for block in range(block_count):
                segment = record_of_suffix[block * block_size : (block + 1) * block_size]
                distinct = np.unique(segment)[:width]
                summaries[block, : distinct.shape[0]] = distinct
                watcher.update(current=block + 1)
            del record_of_suffix

        return cls(summaries, positions, starts, block_size, width)

    def smallest_record_ids(
        self,
        low: int,
        high: int,
        *,
        need: int,
        exclude: Collection[int] = (),
    ) -> list[int]:
        """The ``need`` smallest distinct record numbers in a suffix-array range.

        Exact, not approximate: the answer is the same as reading every entry in
        the range. ``need + len(exclude)`` must not exceed :attr:`width`, which
        is the condition the summaries were built to satisfy.

        Args:
            low: First entry of the range, inclusive.
            high: Last entry of the range, exclusive.
            need: How many record numbers to return.
            exclude: Record numbers to skip, typically ones already chosen from
                a higher-scoring tier.
        """
        if need < 1 or high <= low:
            return []
        if need + len(exclude) > self.width:
            raise BlockSummaryError(
                f"asked for {need} record(s) while excluding {len(exclude)}, "
                f"beyond the {self.width} kept per block"
            )

        candidates = np.unique(np.concatenate(self._parts(low, high)))
        excluded = set(exclude)
        chosen: list[int] = []
        for value in candidates:
            record = int(value)
            if record == SENTINEL:
                break  # sentinels sort last, so nothing usable follows
            if record not in excluded:
                chosen.append(record)
                if len(chosen) == need:
                    break
        return chosen

    def _parts(self, low: int, high: int) -> list[np.ndarray]:
        """Split a range into summarized whole blocks and exact end fragments."""
        size = self.block_size
        first_whole = -(-low // size)
        last_whole = high // size

        if first_whole >= last_whole:
            # Narrower than two blocks, so reading it exactly is already cheap.
            return [self._records_in(low, high)]

        parts = [self.summaries[first_whole:last_whole].ravel()]
        if low < first_whole * size:
            parts.append(self._records_in(low, first_whole * size))
        if last_whole * size < high:
            parts.append(self._records_in(last_whole * size, high))
        return parts

    def _records_in(self, low: int, high: int) -> np.ndarray:
        """Record numbers of every entry in a range, read exactly.

        Only ever called on fragments shorter than two blocks, so this maps at
        most a few thousand positions however large the original range was.
        """
        return np.searchsorted(self.starts, self.positions[low:high], side="right") - 1

    def write_to(self, directory: Path | str) -> None:
        """Write the summaries into ``directory``."""
        np.save(Path(directory) / BLOCK_SUMMARY_FILE, self.summaries, allow_pickle=False)

    @classmethod
    def read_from(
        cls,
        directory: Path | str,
        positions: np.ndarray,
        starts: np.ndarray,
        *,
        width: int,
        block_size: int,
        use_mmap: bool = True,
    ) -> "BlockSummaries":
        """Read summaries written by :meth:`write_to` and check they fit."""
        summaries = np.load(
            Path(directory) / BLOCK_SUMMARY_FILE,
            mmap_mode="r" if use_mmap else None,
            allow_pickle=False,
        )
        blocks = cls(summaries, positions, starts, block_size, width)
        blocks.check_structure()
        return blocks

    def check_structure(self) -> None:
        """Check the summaries describe the suffix array they were loaded beside."""
        if self.summaries.ndim != 2:
            raise BlockSummaryError(
                f"summaries must be two-dimensional, got {self.summaries.ndim}"
            )
        expected_blocks = -(-int(self.positions.shape[0]) // self.block_size)
        if self.summaries.shape != (expected_blocks, self.width):
            raise BlockSummaryError(
                f"summaries have shape {self.summaries.shape}, expected "
                f"({expected_blocks}, {self.width}) for this suffix array"
            )
