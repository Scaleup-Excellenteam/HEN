"""Finding and reading the corpus files.

The corpus is a directory tree of ``.txt`` files at any depth, and a sentence is
one full line of one file. This module is the only place that touches those
files, so decisions about traversal order, decoding and line numbering live in
one place.

Traversal is deterministic: files are visited in sorted order of their path
relative to the corpus root. Record identities in the index depend on that
order, so it must not vary between runs or machines.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .progress import NULL_SINK, BuildPhase, ProgressSink

__all__ = [
    "CORPUS_SUFFIX",
    "CorpusFile",
    "CorpusNotFoundError",
    "fingerprint",
    "iter_files",
    "iter_lines",
    "read_lines",
]

CORPUS_SUFFIX = ".txt"

#: Files are read as bytes and decoded with this policy. The assignment says the
#: corpus is English, but real files carry stray bytes; replacing them keeps a
#: line readable instead of failing the whole build.
_DECODE_ERRORS = "replace"


class CorpusNotFoundError(FileNotFoundError):
    """Raised when the configured corpus directory does not exist."""


@dataclass(frozen=True)
class CorpusFile:
    """One corpus file.

    Attributes:
        path: Where to read it from.
        source_text: Its path relative to the corpus root, POSIX style. This is
            what completions report, so it must not contain machine-specific
            directories.
    """

    path: Path
    source_text: str


def iter_files(root: Path | str) -> Iterator[CorpusFile]:
    """Yield every corpus file under ``root``, ordered by relative path.

    Directory symlinks are not followed, so a looping tree cannot hang the build.

    Raises:
        CorpusNotFoundError: if ``root`` is missing or is not a directory.
    """
    root = Path(root)
    if not root.is_dir():
        raise CorpusNotFoundError(
            f"corpus directory not found: {root}. Set corpus_root in config.yaml "
            f"to the directory holding the extracted text files."
        )

    found: list[CorpusFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in filenames:
            if not filename.endswith(CORPUS_SUFFIX):
                continue
            path = Path(dirpath) / filename
            found.append(CorpusFile(path, path.relative_to(root).as_posix()))

    # Sorting on the relative path, rather than relying on walk order, gives one
    # canonical ordering whatever the filesystem returns.
    found.sort(key=lambda corpus_file: corpus_file.source_text)
    yield from found


def iter_lines(data: bytes) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, text)`` for a file's raw bytes, numbering from 1.

    Lines are split on ``\\n`` and a trailing ``\\r`` is dropped, so a file with
    Windows endings produces the same sentences as one with Unix endings.
    """
    for number, raw_line in enumerate(data.split(b"\n"), start=1):
        yield number, raw_line.rstrip(b"\r").decode("utf-8", errors=_DECODE_ERRORS)


def read_lines(corpus_file: CorpusFile) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, text)`` for one corpus file."""
    return iter_lines(corpus_file.path.read_bytes())


def fingerprint(root: Path | str, sink: ProgressSink | None = None) -> str:
    """Hash the corpus content, for detecting that a cache is out of date.

    Covers both the set of files and their contents, so an edit that leaves a
    file's size and timestamp unchanged is still noticed. Each path and each
    file's bytes are length-prefixed before hashing, so no combination of names
    and contents can be rearranged into the same byte stream.

    ``sink`` receives real progress: the walk is indeterminate until the file
    count is known, and determinate by file afterwards. Every path reported is
    the corpus-relative one, never the path on disk.
    """
    watcher = sink or NULL_SINK
    watcher.begin(
        BuildPhase.VALIDATING_CORPUS,
        detail="Hashing the corpus to check the cached index against it.",
        determinate=False,
    )

    files = list(iter_files(root))
    watcher.update(
        total=len(files),
        files_total=len(files),
        bytes_total=sum(corpus_file.path.stat().st_size for corpus_file in files)
        if sink is not None
        else None,
        detail=f"Hashing {len(files):,} corpus files.",
    )

    digest = hashlib.sha256()
    processed = 0
    for position, corpus_file in enumerate(files, start=1):
        encoded_path = corpus_file.source_text.encode("utf-8")
        data = corpus_file.path.read_bytes()
        digest.update(len(encoded_path).to_bytes(4, "little"))
        digest.update(encoded_path)
        digest.update(len(data).to_bytes(8, "little"))
        digest.update(data)
        processed += len(data)
        watcher.update(
            current=position,
            files_done=position,
            bytes_done=processed,
            current_file=corpus_file.source_text,
        )
    return digest.hexdigest()
