"""The record store: every corpus sentence, laid out for searching.

A record is one corpus line that survives normalization. The store keeps, for
each record, the original text to show the user, the normalized text to search,
and where the line came from.

Two decisions in the layout carry the design:

**Records are sorted by the result tie-break key.** Equal-scoring completions are
ordered by :func:`~autocomplete.data.tie_break_key`, so storing records in that
order makes a record's position its rank. Choosing between thousands of
equally-scoring matches then means taking the smallest record numbers, which is
a cheap operation on sorted integers rather than a sort of the matches.

**Normalized text is concatenated into one blob**, records separated by a
newline. The searchable text is therefore a single string, which is what the
suffix array is built over. Since normalization can never emit a newline, and
every search pattern is normalized text, a match can never span two records.
That is what makes the separator a boundary rather than a convention.
"""

from __future__ import annotations

import json
import mmap
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Union

import numpy as np

from . import corpus
from .data import AutoCompleteData, tie_break_key
from .normalize import ALPHABET, normalize
from .progress import NULL_SINK, BuildPhase, ProgressSink

__all__ = [
    "ARTIFACT_FILES",
    "RECORD_SEPARATOR",
    "RecordStore",
    "RecordStoreError",
]

#: Separates records inside the normalized blob. Outside the normalized
#: alphabet by construction, which is what makes it a hard boundary.
RECORD_SEPARATOR = b"\n"

_ARRAY_FILES = {
    "starts": "starts.npy",
    "orig_starts": "orig_starts.npy",
    "file_id": "file_id.npy",
    "line_no": "line_no.npy",
}
_BLOB_FILES = {
    "norm_blob": "norm_blob.bin",
    "orig_blob": "orig_blob.bin",
}
_PATHS_FILE = "paths.json"

#: Every file the store writes, for the cache to checksum and validate.
ARTIFACT_FILES = tuple(
    sorted([*_ARRAY_FILES.values(), *_BLOB_FILES.values(), _PATHS_FILE])
)

# Positions into the normalized blob are held as int32, matching the suffix
# array that will index it; a corpus large enough to overflow that is rejected
# rather than silently truncated.
_MAX_BLOB_BYTES = 2**31 - 1

Buffer = Union[bytes, mmap.mmap]


class RecordStoreError(RuntimeError):
    """Raised when a store cannot be built or read back."""


@dataclass(frozen=True)
class RecordStore:
    """Corpus sentences in searchable form.

    Records are numbered from 0 in tie-break order. Every array is indexed by
    that record number, except ``starts`` and ``orig_starts`` which hold one
    extra entry so that record ``i`` occupies ``[starts[i], starts[i + 1] - 1)``.
    """

    norm_blob: Buffer
    starts: np.ndarray
    orig_blob: Buffer
    orig_starts: np.ndarray
    file_id: np.ndarray
    line_no: np.ndarray
    paths: tuple[str, ...]

    def __len__(self) -> int:
        """The number of records."""
        return int(self.file_id.shape[0])

    @property
    def max_record_length(self) -> int:
        """Length of the longest normalized record.

        The search uses this to reject a query too long to match anything, and
        to bound how often one pattern can occur inside a single record.
        """
        if len(self) == 0:
            return 0
        return int(np.diff(self.starts).max()) - 1

    def normalized(self, record: int) -> bytes:
        """The normalized text of a record."""
        return bytes(self.norm_blob[self.starts[record] : self.starts[record + 1] - 1])

    def sentence(self, record: int) -> str:
        """The record's line as it appears in the file, punctuation and all."""
        start = int(self.orig_starts[record])
        stop = int(self.orig_starts[record + 1]) - 1
        return bytes(self.orig_blob[start:stop]).decode("utf-8")

    def source_text(self, record: int) -> str:
        """The record's file, relative to the corpus root."""
        return self.paths[int(self.file_id[record])]

    def offset(self, record: int) -> int:
        """The record's 1-based line number within its file."""
        return int(self.line_no[record])

    def completion(self, record: int, score: int) -> AutoCompleteData:
        """Build the result a caller sees for this record."""
        return AutoCompleteData(
            self.sentence(record),
            self.source_text(record),
            self.offset(record),
            score,
        )

    def record_at(self, position: int) -> int:
        """Which record contains a given position in the normalized blob."""
        return int(np.searchsorted(self.starts, position, side="right")) - 1

    def records_at(self, positions: np.ndarray) -> np.ndarray:
        """Vectorized :meth:`record_at`, for a whole array of blob positions."""
        return np.searchsorted(self.starts, positions, side="right") - 1

    @classmethod
    def build(cls, root: Path | str, sink: ProgressSink | None = None) -> "RecordStore":
        """Read a corpus directory and lay it out for searching.

        Lines that normalize to nothing are dropped: they cannot match any query,
        so indexing them would only cost space.

        ``sink`` receives the file being read, how many are done, how many
        sentences have been kept and how many bytes have been consumed. All of
        those are counted, not estimated, and the path reported is always the
        corpus-relative one.
        """
        watcher = sink or NULL_SINK

        watcher.begin(
            BuildPhase.DISCOVERING_CORPUS,
            detail="Walking the corpus directory.",
            determinate=False,
        )
        files = list(corpus.iter_files(root))
        paths = tuple(corpus_file.source_text for corpus_file in files)

        # Only stat the tree when somebody is watching: it is a syscall per file
        # that buys nothing except a byte total to show.
        total_bytes = (
            sum(corpus_file.path.stat().st_size for corpus_file in files)
            if sink is not None
            else None
        )
        watcher.begin(
            BuildPhase.READING_FILES,
            detail=f"Reading {len(files):,} corpus files.",
            total=len(files),
        )
        watcher.update(files_total=len(files), bytes_total=total_bytes)

        collected: list[tuple[str, int, int, bytes]] = []
        consumed = 0
        for file_index, corpus_file in enumerate(files):
            # Read the bytes here rather than through corpus.read_lines, so the
            # size of each file is known without a second syscall to ask for it.
            data = corpus_file.path.read_bytes()
            for line_number, text in corpus.iter_lines(data):
                normalized = normalize(text)
                if normalized:
                    collected.append((text, file_index, line_number, normalized))
            consumed += len(data)
            watcher.update(
                current=file_index + 1,
                files_done=file_index + 1,
                current_file=corpus_file.source_text,
                sentences=len(collected),
                bytes_done=consumed,
            )

        watcher.begin(
            BuildPhase.NORMALIZING_RECORDS,
            detail=f"Ordering {len(collected):,} sentences.",
            determinate=False,
        )
        watcher.update(sentences=len(collected), files_done=len(files), files_total=len(files))
        collected.sort(
            key=lambda record: tie_break_key(record[0], paths[record[1]], record[2])
        )
        return cls._from_sorted(collected, paths)

    @classmethod
    def _from_sorted(
        cls, collected: list[tuple[str, int, int, bytes]], paths: tuple[str, ...]
    ) -> "RecordStore":
        count = len(collected)
        norm_blob = RECORD_SEPARATOR.join(record[3] for record in collected)
        if count:
            norm_blob += RECORD_SEPARATOR
        if len(norm_blob) > _MAX_BLOB_BYTES:
            raise RecordStoreError(
                f"normalized corpus is {len(norm_blob)} bytes, over the "
                f"{_MAX_BLOB_BYTES} byte limit of the 32-bit position index"
            )

        encoded = [record[0].encode("utf-8") for record in collected]
        orig_blob = RECORD_SEPARATOR.join(encoded)
        if count:
            orig_blob += RECORD_SEPARATOR

        # Summed as int64 and narrowed afterwards: the blob size was checked
        # above, so the totals fit, but the running sum must not overflow while
        # it is being computed.
        starts = _offsets(
            (len(record[3]) + 1 for record in collected), count
        ).astype(np.int32)
        orig_starts = _offsets((len(item) + 1 for item in encoded), count)

        store = cls(
            norm_blob=norm_blob,
            starts=starts,
            orig_blob=orig_blob,
            orig_starts=orig_starts,
            file_id=np.fromiter(
                (record[1] for record in collected), dtype=np.uint32, count=count
            ),
            line_no=np.fromiter(
                (record[2] for record in collected), dtype=np.int32, count=count
            ),
            paths=paths,
        )
        store.check_invariants()
        return store

    def check_invariants(self) -> None:
        """Verify every property the search relies on.

        Run on each build: it turns a silent wrong answer later into a loud
        failure here, and costs a fraction of the build.
        """
        self.check_structure()
        self.check_alphabet()

    def check_structure(self) -> None:
        """Check the arrays agree with each other and with the blobs.

        Cheap, so it also runs when a store is read back from disk.
        """
        count = len(self)
        if self.starts.shape != (count + 1,) or self.orig_starts.shape != (count + 1,):
            raise RecordStoreError("offset arrays must hold one entry per record plus one")
        if self.line_no.shape != (count,):
            raise RecordStoreError("line numbers must hold one entry per record")
        if int(self.starts[0]) != 0 or int(self.orig_starts[0]) != 0:
            raise RecordStoreError("offset arrays must start at zero")
        if int(self.starts[count]) != len(self.norm_blob):
            raise RecordStoreError("normalized offsets do not span the blob")
        if int(self.orig_starts[count]) != len(self.orig_blob):
            raise RecordStoreError("original offsets do not span the blob")
        if count and int(self.file_id.max()) >= len(self.paths):
            raise RecordStoreError("a record refers to a file that is not listed")

    def check_alphabet(self) -> None:
        """Check the searchable blob holds nothing but alphabet and separators.

        The search proof assumes it, so it is verified rather than trusted: a
        stray byte would let a pattern match across a record boundary. One
        C-level pass over the blob, not a check per record.
        """
        leftover = bytes(self.norm_blob).translate(None, ALPHABET + RECORD_SEPARATOR)
        if leftover:
            unexpected = ", ".join(f"0x{value:02x}" for value in sorted(set(leftover))[:8])
            raise RecordStoreError(
                f"normalized blob contains bytes outside the alphabet: {unexpected}"
            )

    def write_to(self, directory: Path | str) -> None:
        """Write every artifact into ``directory``, which must already exist."""
        directory = Path(directory)
        for attribute, filename in _ARRAY_FILES.items():
            np.save(directory / filename, getattr(self, attribute), allow_pickle=False)
        for attribute, filename in _BLOB_FILES.items():
            (directory / filename).write_bytes(bytes(getattr(self, attribute)))
        (directory / _PATHS_FILE).write_text(
            json.dumps(list(self.paths), ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def read_from(cls, directory: Path | str, use_mmap: bool = True) -> "RecordStore":
        """Read a store previously written by :meth:`write_to`.

        With ``use_mmap`` the arrays and blobs stay on disk and are paged in as
        they are touched, so start-up does not depend on the size of the corpus.
        """
        directory = Path(directory)
        arrays = {
            attribute: np.load(
                directory / filename,
                mmap_mode="r" if use_mmap else None,
                allow_pickle=False,
            )
            for attribute, filename in _ARRAY_FILES.items()
        }
        blobs = {
            attribute: _read_blob(directory / filename, use_mmap)
            for attribute, filename in _BLOB_FILES.items()
        }
        paths = json.loads((directory / _PATHS_FILE).read_text(encoding="utf-8"))
        return cls(paths=tuple(paths), **arrays, **blobs)


def _offsets(lengths, count: int) -> np.ndarray:
    """Turn record lengths into the start offset of each record, plus an end."""
    offsets = np.zeros(count + 1, dtype=np.int64)
    np.cumsum(np.fromiter(lengths, dtype=np.int64, count=count), out=offsets[1:])
    return offsets


def _read_blob(path: Path, use_mmap: bool) -> Buffer:
    """Read a blob, memory-mapping it when asked and when it is not empty."""
    if not use_mmap or path.stat().st_size == 0:
        return path.read_bytes()
    descriptor = os.open(path, os.O_RDONLY)
    try:
        return mmap.mmap(descriptor, 0, access=mmap.ACCESS_READ)
    finally:
        os.close(descriptor)
