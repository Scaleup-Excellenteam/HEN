"""Everything needed to answer a query, built and persisted as one unit.

An index is the record store plus the structures that search it: the suffix
array over the normalized blob, and the per-block summaries that pick winners
out of a large range. They are built, written, validated and adopted together,
because a suffix array is only meaningful for the exact blob it was built from
and a summary is only meaningful for the exact suffix array it describes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .data import TIE_BREAK_POLICY
from .normalize import DEFAULT_PUNCTUATION_POLICY
from .progress import NULL_SINK, BuildPhase, IndexStats, ProgressSink
from .records import ARTIFACT_FILES as RECORD_FILES
from .records import RecordStore
from .suffix_index import SUFFIX_ARRAY_FILE, SuffixIndex
from .topk import BLOCK_SUMMARY_FILE, DEFAULT_BLOCK_SIZE, BlockSummaries

__all__ = ["ARTIFACT_FILES", "SearchIndex"]

#: Every file a complete index writes.
ARTIFACT_FILES = tuple(sorted([*RECORD_FILES, SUFFIX_ARRAY_FILE, BLOCK_SUMMARY_FILE]))

Logger = Callable[[str], None]


@dataclass(frozen=True)
class SearchIndex:
    """A record store with its search structures."""

    records: RecordStore
    suffix: SuffixIndex
    blocks: BlockSummaries

    def __len__(self) -> int:
        return len(self.records)

    @property
    def summary_width(self) -> int:
        """How many results a query may ask for, K in the design."""
        return self.blocks.width

    @property
    def block_size(self) -> int:
        return self.blocks.block_size

    @classmethod
    def build(
        cls,
        root: Path | str,
        *,
        summary_width: int,
        block_size: int = DEFAULT_BLOCK_SIZE,
        log: Logger | None = None,
        sink: ProgressSink | None = None,
    ) -> "SearchIndex":
        """Read a corpus and prepare every structure needed to search it.

        ``log`` is the plain text logger the command line prints; ``sink`` is the
        structured one an interface watches. They are independent, and both are
        optional, so nothing about the command line changes when nothing is
        watching and nothing about watching changes what is printed.
        """
        announce = log or (lambda message: None)
        watcher = sink or NULL_SINK

        started = time.perf_counter()
        records = RecordStore.build(root, sink)
        announce(
            f"read {len(records):,} sentences from {len(records.paths):,} files "
            f"in {time.perf_counter() - started:.1f}s"
        )

        started = time.perf_counter()
        # The suffix array is one call into a C library that reports nothing
        # part-way, so this phase is indeterminate by construction. What is
        # honest to show is how much text it is ordering, which is known.
        watcher.begin(
            BuildPhase.BUILDING_SUFFIX_ARRAY,
            detail=(
                f"Ordering every suffix of {len(records.norm_blob) / 1e6:.1f} MB "
                f"of normalized text."
            ),
            determinate=False,
        )
        watcher.update(bytes_total=len(records.norm_blob), bytes_done=0)
        suffix = SuffixIndex.build(records.norm_blob, records.max_record_length)
        announce(
            f"built the suffix array over {len(records.norm_blob) / 1e6:.1f} MB "
            f"in {time.perf_counter() - started:.1f}s"
        )

        started = time.perf_counter()
        blocks = BlockSummaries.build(
            suffix.positions,
            records.starts,
            width=summary_width,
            block_size=block_size,
            sink=sink,
        )
        announce(
            f"summarized {blocks.summaries.shape[0]:,} blocks of {block_size} "
            f"in {time.perf_counter() - started:.1f}s"
        )
        return cls(records, suffix, blocks)

    def write_to(self, directory: Path | str) -> None:
        """Write every artifact into ``directory``."""
        self.records.write_to(directory)
        self.suffix.write_to(directory)
        self.blocks.write_to(directory)

    @classmethod
    def read_from(
        cls,
        directory: Path | str,
        *,
        summary_width: int,
        block_size: int,
        use_mmap: bool = True,
    ) -> "SearchIndex":
        """Read an index written by :meth:`write_to`, checking the parts agree."""
        records = RecordStore.read_from(directory, use_mmap=use_mmap)
        suffix = SuffixIndex.read_from(
            directory,
            records.norm_blob,
            records.max_record_length,
            use_mmap=use_mmap,
        )
        blocks = BlockSummaries.read_from(
            directory,
            suffix.positions,
            records.starts,
            width=summary_width,
            block_size=block_size,
            use_mmap=use_mmap,
        )
        return cls(records, suffix, blocks)

    def check_structure(self) -> None:
        """Check every part is consistent with the others."""
        self.records.check_structure()
        self.suffix.check_structure()
        self.blocks.check_structure()

    def stats(self) -> IndexStats:
        """What this index is, for an interface to report once it is ready."""
        return IndexStats(
            sentences=len(self.records),
            files=len(self.records.paths),
            searchable_bytes=len(self.records.norm_blob),
            longest_sentence=self.records.max_record_length,
            suffix_positions=int(self.suffix.positions.shape[0]),
            block_count=int(self.blocks.summaries.shape[0]),
            block_size=self.block_size,
            summary_width=self.summary_width,
        )

    def describe(self) -> dict:
        """The facts a cache manifest needs to decide this index is reusable.

        Anything that changes what the artifacts mean belongs here, so that an
        index built under different settings is rejected rather than served.
        """
        return {
            "record_count": len(self.records),
            "file_count": len(self.records.paths),
            "max_record_length": self.records.max_record_length,
            "normalized_bytes": len(self.records.norm_blob),
            "block_size": self.block_size,
            "summary_width": self.summary_width,
            "tie_break": TIE_BREAK_POLICY,
            "punctuation_policy": DEFAULT_PUNCTUATION_POLICY.value,
            "arrays": {
                "starts": _array_shape(self.records.starts),
                "orig_starts": _array_shape(self.records.orig_starts),
                "file_id": _array_shape(self.records.file_id),
                "line_no": _array_shape(self.records.line_no),
                "suffix_array": _array_shape(self.suffix.positions),
                "block_summaries": _array_shape(self.blocks.summaries),
            },
        }


def _array_shape(array) -> dict:
    return {"dtype": str(array.dtype), "shape": list(array.shape)}
