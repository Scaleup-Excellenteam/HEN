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

import numpy as np

from . import corpus
from .config import VALIDATION_LEVELS, Config
from .normalize import DEFAULT_PUNCTUATION_POLICY
from .records import ARTIFACT_FILES, RecordStore

__all__ = [
    "CacheError",
    "CacheMiss",
    "FORMAT_VERSION",
    "MANIFEST_FILE",
    "POINTER_FILE",
    "build_or_load",
    "load",
    "save",
]

#: Bumped whenever the on-disk layout or a decision baked into it changes, so an
#: older cache is rejected instead of misread.
FORMAT_VERSION = 1

POINTER_FILE = "CURRENT"
MANIFEST_FILE = "manifest.json"
_GENERATION_PREFIX = "gen-"
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


def save(store: RecordStore, cache_dir: Path | str, corpus_hash: str) -> Path:
    """Write ``store`` as a new generation and make it the current one.

    Returns the generation directory.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    generation = f"{_GENERATION_PREFIX}{corpus_hash[:12]}-{uuid.uuid4().hex[:8]}"
    generation_dir = cache_dir / generation
    generation_dir.mkdir()

    store.write_to(generation_dir)
    manifest = _build_manifest(store, corpus_hash, generation_dir)
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
) -> RecordStore:
    """Read the current cache, refusing anything that cannot be trusted.

    Args:
        cache_dir: Directory holding the generations and the pointer.
        corpus_hash: Fingerprint of the corpus as it is now. Required for the
            ``content`` and ``full`` levels, which use it to notice edits.
        level: How much to check. ``structural`` reads the manifest and the
            array shapes; ``content`` also compares the corpus fingerprint;
            ``full`` also re-hashes every stored file, for use after a crash.
        use_mmap: Leave the artifacts on disk and page them in on demand.

    Raises:
        CacheMiss: if there is no cache, or it is stale, damaged or foreign.
    """
    if level not in VALIDATION_LEVELS:
        raise ValueError(f"unknown validation level {level!r}")
    if level in ("content", "full") and corpus_hash is None:
        raise ValueError(f"the {level!r} validation level needs the corpus hash")

    cache_dir = Path(cache_dir)
    generation_dir = _current_generation(cache_dir)
    manifest = _read_manifest(generation_dir)

    if manifest.get("format_version") != FORMAT_VERSION:
        raise CacheMiss(
            f"cache was written by format version {manifest.get('format_version')!r}, "
            f"this build expects {FORMAT_VERSION}"
        )
    if manifest.get("punctuation_policy") != DEFAULT_PUNCTUATION_POLICY.value:
        raise CacheMiss(
            "cache was built with punctuation policy "
            f"{manifest.get('punctuation_policy')!r}, now using "
            f"{DEFAULT_PUNCTUATION_POLICY.value!r}"
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
        store = RecordStore.read_from(generation_dir, use_mmap=use_mmap)
        _check_arrays(store, manifest)
        store.check_structure()
    except CacheMiss:
        raise
    except Exception as exc:  # unreadable arrays, bad JSON, truncated blobs
        raise CacheMiss(f"cached index could not be read: {exc}") from exc

    if len(store) != manifest.get("record_count"):
        raise CacheMiss("cached index holds a different number of records")
    return store


def build_or_load(
    config: Config,
    *,
    force_rebuild: bool = False,
    log: Logger | None = None,
) -> RecordStore:
    """Return a ready index, reusing the cache when it is still valid."""
    announce = log or (lambda message: None)

    corpus_hash: str | None = None
    if config.validation_level in ("content", "full"):
        corpus_hash = corpus.fingerprint(config.corpus_root)

    if not force_rebuild:
        try:
            store = load(
                config.cache_dir,
                corpus_hash=corpus_hash,
                level=config.validation_level,
                use_mmap=config.use_mmap,
            )
        except CacheMiss as reason:
            announce(f"building the index ({reason})")
        else:
            announce(f"loaded {len(store)} records from cache")
            return store
    else:
        announce("building the index (rebuild requested)")

    if corpus_hash is None:
        corpus_hash = corpus.fingerprint(config.corpus_root)
    store = RecordStore.build(config.corpus_root)
    save(store, config.cache_dir, corpus_hash)
    announce(f"indexed {len(store)} records from {len(store.paths)} files")
    return store


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


def _build_manifest(store: RecordStore, corpus_hash: str, generation_dir: Path) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "corpus_hash": corpus_hash,
        "punctuation_policy": DEFAULT_PUNCTUATION_POLICY.value,
        "record_count": len(store),
        "file_count": len(store.paths),
        "max_record_length": store.max_record_length,
        "arrays": {
            name: {
                "dtype": str(getattr(store, name).dtype),
                "shape": list(getattr(store, name).shape),
            }
            for name in ("starts", "orig_starts", "file_id", "line_no")
        },
        "artifacts": {
            filename: {
                "bytes": (generation_dir / filename).stat().st_size,
                "sha256": _sha256(generation_dir / filename),
            }
            for filename in ARTIFACT_FILES
        },
    }


def _check_arrays(store: RecordStore, manifest: dict) -> None:
    described = manifest.get("arrays")
    if not isinstance(described, dict):
        raise CacheMiss("manifest does not describe the index arrays")
    for name, expected in described.items():
        array = getattr(store, name, None)
        if not isinstance(array, np.ndarray):
            raise CacheMiss(f"cached index is missing the {name} array")
        if str(array.dtype) != expected.get("dtype"):
            raise CacheMiss(
                f"{name} has type {array.dtype}, manifest says {expected.get('dtype')}"
            )
        if list(array.shape) != list(expected.get("shape", [])):
            raise CacheMiss(
                f"{name} has shape {list(array.shape)}, manifest says "
                f"{expected.get('shape')}"
            )


def _point_at(cache_dir: Path, generation: str) -> None:
    """Publish a generation by renaming the pointer onto it."""
    temporary = cache_dir / f"{_POINTER_TEMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:6]}"
    temporary.write_text(f"{generation}\n", encoding="utf-8")
    _flush_file(temporary)
    os.replace(temporary, cache_dir / POINTER_FILE)
    _flush_directory(cache_dir)


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
