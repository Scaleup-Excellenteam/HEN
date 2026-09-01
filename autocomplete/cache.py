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
    "GENERATION_PREFIX",
    "MANIFEST_FILE",
    "POINTER_FILE",
    "build_or_load",
    "current_generation",
    "discard_other_generations",
    "flush_directory",
    "load",
    "new_generation_name",
    "publish_pointer",
    "save",
]

#: Bumped whenever the on-disk layout or a decision baked into it changes, so an
#: older cache is rejected instead of misread.
FORMAT_VERSION = 2

POINTER_FILE = "CURRENT"
MANIFEST_FILE = "manifest.json"

#: Names a self-contained generation directory. Public because the generation
#: and pointer discipline below is reused for other atomically published
#: directories, not only the corpus index.
GENERATION_PREFIX = "gen-"

_POINTER_TEMP_PREFIX = f"{POINTER_FILE}.tmp-"

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

    generation = new_generation_name(corpus_hash)
    generation_dir = cache_dir / generation
    generation_dir.mkdir()

    index.write_to(generation_dir)
    manifest = _build_manifest(index, corpus_hash, generation_dir)
    (generation_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    flush_directory(generation_dir)
    publish_pointer(cache_dir, generation)
    discard_other_generations(cache_dir, keep=generation)
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
    if level not in VALIDATION_LEVELS:
        raise ValueError(f"unknown validation level {level!r}")
    if level in ("content", "full") and corpus_hash is None:
        raise ValueError(f"the {level!r} validation level needs the corpus hash")

    cache_dir = Path(cache_dir)
    generation_dir = current_generation(cache_dir)
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


def build_or_load(
    config: Config,
    *,
    force_rebuild: bool = False,
    block_size: int = DEFAULT_BLOCK_SIZE,
    log: Logger | None = None,
) -> SearchIndex:
    """Return a ready index, reusing the cache when it is still valid."""
    announce = log or (lambda message: None)

    corpus_hash: str | None = None
    if config.validation_level in ("content", "full"):
        corpus_hash = corpus.fingerprint(config.corpus_root)

    if not force_rebuild:
        try:
            index = load(
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
            return index
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
    generation = save(index, config.cache_dir, corpus_hash)
    announce(f"wrote generation {generation.name}")
    return index


def new_generation_name(fingerprint: str) -> str:
    """A fresh generation directory name, tagged with what it was built from.

    The random suffix is what lets two builds of the same input write separate
    directories instead of one overwriting the other's half-written files.
    """
    return f"{GENERATION_PREFIX}{fingerprint[:12]}-{uuid.uuid4().hex[:8]}"


def current_generation(directory: Path | str) -> Path:
    """The generation the pointer in ``directory`` names.

    Raises:
        CacheMiss: if there is no pointer, or it does not name a generation
            directory that exists.
    """
    directory = Path(directory)
    pointer = directory / POINTER_FILE
    try:
        named = pointer.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CacheMiss(f"no usable cache in {directory}") from exc

    # The pointer is data on disk: never let it address anything but a
    # generation directly inside the cache directory.
    if not named.startswith(GENERATION_PREFIX) or named != Path(named).name:
        raise CacheMiss(f"cache pointer does not name a generation: {named!r}")

    generation_dir = directory / named
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


def publish_pointer(directory: Path | str, generation: str) -> None:
    """Publish a generation by renaming the pointer onto it.

    Renaming one file is atomic, so a reader sees either the previous
    generation or this one, never a mixture of the two.
    """
    directory = Path(directory)
    temporary = directory / f"{_POINTER_TEMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:6]}"
    temporary.write_text(f"{generation}\n", encoding="utf-8")
    _flush_file(temporary)
    os.replace(temporary, directory / POINTER_FILE)
    _flush_directory(directory)


def discard_other_generations(cache_dir: Path | str, keep: str) -> None:
    """Remove superseded generations and abandoned pointer files.

    Failures are ignored: tidying up must never break a build that has already
    published its result.
    """
    cache_dir = Path(cache_dir)
    for entry in cache_dir.iterdir():
        if entry.name == keep:
            continue
        stale_generation = entry.is_dir() and entry.name.startswith(GENERATION_PREFIX)
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


def flush_directory(directory: Path | str) -> None:
    """Make a directory tree's contents durable before anything points at it."""
    directory = Path(directory)
    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            flush_directory(entry)
        elif entry.is_file():
            _flush_file(entry)
    _flush_directory(directory)


def _flush_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _flush_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError:
        # Not every platform allows syncing a directory handle.
        pass
    finally:
        os.close(descriptor)
