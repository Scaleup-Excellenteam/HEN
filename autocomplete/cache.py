"""Storing a built index on disk, and deciding whether a stored one may be used.

Building the index costs seconds on the real corpus; serving from a cached one
costs well under a second. That is only worth doing if a stale or damaged cache
can never be mistaken for a good one, so a cache is written as a self-contained
*generation* directory and adopted by a single atomic step:

1. Write the whole generation into ``cache_dir/gen-<hash>-<random>/``.
2. Flush it to disk.
3. Rename a small pointer file, ``CURRENT``, to name that generation.

Renaming one file is atomic, so a reader sees either the previous generation or
the new one, never a half-written mixture. A build that dies at any point leaves
the old pointer, and therefore the old cache, untouched; the abandoned directory
is removed by the next successful build. Two builds running at once write
separate generations and the later pointer wins, which is correct because both
describe the same corpus.

Removing an old generation is safe while a reader still has it memory-mapped:
on POSIX the files stay alive until that reader closes them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from . import corpus
from .config import VALIDATION_LEVELS, Config
from .data import TIE_BREAK_POLICY
from .index import ARTIFACT_FILES, SearchIndex
from .normalize import DEFAULT_PUNCTUATION_POLICY
from .topk import DEFAULT_BLOCK_SIZE

__all__ = [
    "CacheError",
    "CacheMiss",
    "FORMAT_VERSION",
    "MANIFEST_FILE",
    "POINTER_FILE",
    "build_or_load",
    "build_or_load_current",
    "current_generation_name",
    "load",
    "load_current",
    "save",
]

#: Bumped whenever the on-disk layout or a decision baked into it changes, so an
#: older cache is rejected instead of misread.
FORMAT_VERSION = 2

POINTER_FILE = "CURRENT"
MANIFEST_FILE = "manifest.json"
_GENERATION_PREFIX = "gen-"
_POINTER_TEMP_PREFIX = f"{POINTER_FILE}.tmp-"

#: Windows differs from POSIX in what it lets a process do to a file it has
#: just written, in two places below. Named once, so those places read as the
#: platform difference they are.
_IS_WINDOWS = os.name == "nt"

#: How long to keep retrying the pointer rename against a Windows reader
#: holding the file open, and how long to wait between attempts.
_POINTER_REPLACE_TIMEOUT = 2.0
_POINTER_REPLACE_RETRY_DELAY = 0.01

Logger = Callable[[str], None]


class CacheError(RuntimeError):
    """Raised when a cache cannot be written."""


class CacheMiss(CacheError):
    """Raised when no usable cache is present.

    Every reason a cache cannot be trusted, absent, stale, damaged, or written
    by another version, arrives as this, because the response to all of them is
    the same: build again.
    """


def save(index: SearchIndex, cache_dir: Path | str, corpus_hash: str) -> Path:
    """Write ``index`` as a new generation and make it the current one.

    Every artifact is written into a fresh directory, so an index that is
    already published is never modified in place. Returns the generation
    directory.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    generation = f"{_GENERATION_PREFIX}{corpus_hash[:12]}-{uuid.uuid4().hex[:8]}"
    generation_dir = cache_dir / generation
    generation_dir.mkdir()

    index.write_to(generation_dir)
    manifest = _build_manifest(index, corpus_hash, generation_dir)
    (generation_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    _flush(generation_dir)
    _point_at(cache_dir, generation)
    _discard_other_generations(cache_dir, keep=generation)
    return generation_dir


def load(
    cache_dir: Path | str,
    *,
    corpus_hash: str | None = None,
    level: str = "content",
    use_mmap: bool = True,
    summary_width: int,
    block_size: int = DEFAULT_BLOCK_SIZE,
) -> SearchIndex:
    """Read the current cache, refusing anything that cannot be trusted.

    Args:
        cache_dir: Directory holding the generations and the pointer.
        corpus_hash: Fingerprint of the corpus as it is now. Required for the
            ``content`` and ``full`` levels, which use it to notice edits.
        level: How much to check. ``structural`` reads the manifest and the
            array shapes; ``content`` also compares the corpus fingerprint;
            ``full`` also re-hashes every stored file, for use after a crash.
        use_mmap: Leave the artifacts on disk and page them in on demand.
        summary_width: How many results the caller intends to ask for. A cache
            summarizing fewer cannot answer them, so it is rejected.
        block_size: Suffix-array entries per summary row.

    Raises:
        CacheMiss: if there is no cache, or it is stale, damaged or foreign.
    """
    _validate_load_args(corpus_hash, level)
    cache_dir = Path(cache_dir)
    generation_dir = _current_generation(cache_dir)
    return _load_from(
        generation_dir,
        corpus_hash=corpus_hash,
        level=level,
        use_mmap=use_mmap,
        summary_width=summary_width,
        block_size=block_size,
    )


def load_current(
    cache_dir: Path | str,
    *,
    corpus_hash: str | None = None,
    level: str = "content",
    use_mmap: bool = True,
    summary_width: int,
    block_size: int = DEFAULT_BLOCK_SIZE,
) -> tuple[SearchIndex, str]:
    """Like :func:`load`, but also return the generation name that was read.

    ``load`` reads the ``CURRENT`` pointer internally and never reports which
    generation it resolved to, so a caller that wants a label for what it
    loaded has to read the pointer separately, and a new generation
    published in between the two reads can make that label wrong for what
    was actually loaded. This reads the pointer exactly once and returns the
    generation name from that same read, so the label can never disagree
    with the index.

    Raises:
        CacheMiss: if there is no cache, or it is stale, damaged or foreign.
    """
    _validate_load_args(corpus_hash, level)
    cache_dir = Path(cache_dir)
    generation_dir = _current_generation(cache_dir)
    index = _load_from(
        generation_dir,
        corpus_hash=corpus_hash,
        level=level,
        use_mmap=use_mmap,
        summary_width=summary_width,
        block_size=block_size,
    )
    return index, generation_dir.name


def _validate_load_args(corpus_hash: str | None, level: str) -> None:
    if level not in VALIDATION_LEVELS:
        raise ValueError(f"unknown validation level {level!r}")
    if level in ("content", "full") and corpus_hash is None:
        raise ValueError(f"the {level!r} validation level needs the corpus hash")


def _load_from(
    generation_dir: Path,
    *,
    corpus_hash: str | None,
    level: str,
    use_mmap: bool,
    summary_width: int,
    block_size: int,
) -> SearchIndex:
    manifest = _read_manifest(generation_dir)

    if manifest.get("format_version") != FORMAT_VERSION:
        raise CacheMiss(
            f"cache was written by format version {manifest.get('format_version')!r}, "
            f"this build expects {FORMAT_VERSION}"
        )
    for setting, expected in (
        ("punctuation_policy", DEFAULT_PUNCTUATION_POLICY.value),
        ("tie_break", TIE_BREAK_POLICY),
        ("summary_width", summary_width),
        ("block_size", block_size),
    ):
        if manifest.get(setting) != expected:
            raise CacheMiss(
                f"cache was built with {setting} {manifest.get(setting)!r}, "
                f"now using {expected!r}"
            )
    if level in ("content", "full") and manifest.get("corpus_hash") != corpus_hash:
        raise CacheMiss("the corpus has changed since the cache was built")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_FILES):
        raise CacheMiss("manifest does not describe the expected set of files")

    for filename, expected in artifacts.items():
        path = generation_dir / filename
        try:
            actual_size = path.stat().st_size
        except OSError as exc:
            raise CacheMiss(f"cached file {filename} is missing") from exc
        if actual_size != expected.get("bytes"):
            raise CacheMiss(
                f"cached file {filename} is {actual_size} bytes, manifest says "
                f"{expected.get('bytes')}"
            )
        if level == "full" and _sha256(path) != expected.get("sha256"):
            raise CacheMiss(f"cached file {filename} does not match its checksum")

    try:
        index = SearchIndex.read_from(
            generation_dir,
            summary_width=summary_width,
            block_size=block_size,
            use_mmap=use_mmap,
        )
        _check_arrays(index, manifest)
        index.check_structure()
    except CacheMiss:
        raise
    except Exception as exc:  # unreadable arrays, truncated blobs, mismatched shapes
        raise CacheMiss(f"cached index could not be read: {exc}") from exc

    if len(index) != manifest.get("record_count"):
        raise CacheMiss("cached index holds a different number of records")
    return index


def current_generation_name(cache_dir: Path | str) -> str | None:
    """The generation the pointer currently names, or ``None`` to trust none.

    This reads one small file and never touches the index artifacts, so it is
    cheap enough to poll on an interval: a long-running reader uses it to
    notice that a new generation has been published, before paying for the
    full validation :func:`load` performs to actually adopt it.
    """
    try:
        return _current_generation(Path(cache_dir)).name
    except CacheMiss:
        return None


def build_or_load(
    config: Config,
    *,
    force_rebuild: bool = False,
    block_size: int = DEFAULT_BLOCK_SIZE,
    log: Logger | None = None,
) -> SearchIndex:
    """Return a ready index, reusing the cache when it is still valid."""
    index, _ = build_or_load_current(
        config, force_rebuild=force_rebuild, block_size=block_size, log=log
    )
    return index


def build_or_load_current(
    config: Config,
    *,
    force_rebuild: bool = False,
    block_size: int = DEFAULT_BLOCK_SIZE,
    log: Logger | None = None,
) -> tuple[SearchIndex, str]:
    """Like :func:`build_or_load`, but also name the generation it settled on.

    A caller that has to label what it is serving must not read the pointer
    again afterwards: another process can publish in between the load and that
    read, and the label would then name a generation this process never
    loaded. Both paths here report the generation they actually produced, the
    one :func:`load_current` resolved or the one :func:`save` just wrote, so
    the name can never disagree with the index it describes.
    """
    announce = log or (lambda message: None)

    corpus_hash: str | None = None
    if config.validation_level in ("content", "full"):
        corpus_hash = corpus.fingerprint(config.corpus_root)

    if not force_rebuild:
        try:
            index, generation = load_current(
                config.cache_dir,
                corpus_hash=corpus_hash,
                level=config.validation_level,
                use_mmap=config.use_mmap,
                summary_width=config.num_results,
                block_size=block_size,
            )
        except CacheMiss as reason:
            announce(f"building the index ({reason})")
        else:
            announce(f"loaded {len(index)} records from cache")
            return index, generation
    else:
        announce("building the index (rebuild requested)")

    if corpus_hash is None:
        corpus_hash = corpus.fingerprint(config.corpus_root)
    index = SearchIndex.build(
        config.corpus_root,
        summary_width=config.num_results,
        block_size=block_size,
        log=announce,
    )
    generation_dir = save(index, config.cache_dir, corpus_hash)
    announce(f"wrote generation {generation_dir.name}")
    return index, generation_dir.name


def _current_generation(cache_dir: Path) -> Path:
    pointer = cache_dir / POINTER_FILE
    try:
        named = pointer.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CacheMiss(f"no usable cache in {cache_dir}") from exc

    # The pointer is data on disk: never let it address anything but a
    # generation directly inside the cache directory.
    if not named.startswith(_GENERATION_PREFIX) or named != Path(named).name:
        raise CacheMiss(f"cache pointer does not name a generation: {named!r}")

    generation_dir = cache_dir / named
    if not generation_dir.is_dir():
        raise CacheMiss(f"cache pointer names a missing generation: {named}")
    return generation_dir


def _read_manifest(generation_dir: Path) -> dict:
    try:
        manifest = json.loads(
            (generation_dir / MANIFEST_FILE).read_text(encoding="utf-8")
        )
    except OSError as exc:
        raise CacheMiss("cached generation has no manifest") from exc
    except json.JSONDecodeError as exc:
        raise CacheMiss(f"cache manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CacheMiss("cache manifest is not an object")
    return manifest


def _build_manifest(index: SearchIndex, corpus_hash: str, generation_dir: Path) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "corpus_hash": corpus_hash,
        **index.describe(),
        "artifacts": {
            filename: {
                "bytes": (generation_dir / filename).stat().st_size,
                "sha256": _sha256(generation_dir / filename),
            }
            for filename in ARTIFACT_FILES
        },
    }


def _check_arrays(index: SearchIndex, manifest: dict) -> None:
    described = manifest.get("arrays")
    if not isinstance(described, dict):
        raise CacheMiss("manifest does not describe the index arrays")
    actual = index.describe()["arrays"]
    for name, expected in described.items():
        if name not in actual:
            raise CacheMiss(f"cached index is missing the {name} array")
        if actual[name]["dtype"] != expected.get("dtype"):
            raise CacheMiss(
                f"{name} has type {actual[name]['dtype']}, manifest says "
                f"{expected.get('dtype')}"
            )
        if actual[name]["shape"] != list(expected.get("shape", [])):
            raise CacheMiss(
                f"{name} has shape {actual[name]['shape']}, manifest says "
                f"{expected.get('shape')}"
            )


def _point_at(cache_dir: Path, generation: str) -> None:
    """Publish a generation by renaming the pointer onto it."""
    temporary = cache_dir / f"{_POINTER_TEMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:6]}"
    temporary.write_text(f"{generation}\n", encoding="utf-8")
    _flush_file(temporary)
    _replace_pointer(temporary, cache_dir / POINTER_FILE)
    _flush_directory(cache_dir)


def _replace_pointer(temporary: Path, pointer: Path) -> None:
    """Rename the pointer into place, waiting out a concurrent reader.

    ``os.replace`` is atomic on every platform this runs on, but Windows also
    requires nothing else to hold the destination open: Python opens files
    without ``FILE_SHARE_DELETE``, so a reader that happens to be reading
    ``CURRENT`` in that instant makes the rename fail rather than queue. A
    long-running web server polls this very file on an interval, so that
    overlap is rare but expected, and losing an already-built generation to it
    would be absurd. Retrying briefly outlasts a read of a file this small.
    POSIX never takes this path: the rename succeeds regardless of readers.
    """
    deadline = time.monotonic() + _POINTER_REPLACE_TIMEOUT
    while True:
        try:
            os.replace(temporary, pointer)
            return
        except PermissionError:
            if not _IS_WINDOWS or time.monotonic() >= deadline:
                raise
            time.sleep(_POINTER_REPLACE_RETRY_DELAY)


def _discard_other_generations(cache_dir: Path, keep: str) -> None:
    """Remove superseded generations and abandoned pointer files.

    Failures are ignored: tidying up must never break a build that has already
    published its result.
    """
    for entry in cache_dir.iterdir():
        if entry.name == keep:
            continue
        stale_generation = entry.is_dir() and entry.name.startswith(_GENERATION_PREFIX)
        stale_pointer = entry.is_file() and entry.name.startswith(_POINTER_TEMP_PREFIX)
        try:
            if stale_generation:
                shutil.rmtree(entry)
            elif stale_pointer:
                entry.unlink()
        except OSError:
            continue


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _flush(directory: Path) -> None:
    """Make a directory's contents durable before anything points at them."""
    for entry in sorted(directory.iterdir()):
        if entry.is_file():
            _flush_file(entry)
    _flush_directory(directory)


def _flush_file(path: Path) -> None:
    # Windows flushes through the handle, so it needs one opened for writing;
    # POSIX fsyncs the file itself and takes any descriptor. These artifacts
    # were written moments ago, so reopening them read-write always succeeds.
    flags = os.O_RDWR if _IS_WINDOWS else os.O_RDONLY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _flush_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        # Windows has no directory handle to sync: os.open refuses a directory
        # outright, so there is nothing here to do rather than something to
        # skip. The rename that publishes a generation is still atomic.
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Not every platform allows syncing a directory handle.
        pass
    finally:
        os.close(descriptor)
