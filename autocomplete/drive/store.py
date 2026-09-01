"""The imported corpus on disk: its text, its manifest and its overlay index.

This is the feature's whole storage layer, and it is separate from the corpus
cache on purpose. The base corpus is immutable and its index costs seconds to
build; the imported corpus changes whenever someone adds or removes a document
and is small enough to rebuild in full each time. Keeping them apart means an
import can never write, invalidate or damage the base index.

Atomic publication
------------------

A published state is one **generation** directory holding everything that state
consists of: the manifest, the text exactly as it was indexed, and the index
artifacts built from that text. Adopting it is a single ``os.replace`` of the
``CURRENT`` pointer, the same discipline :mod:`autocomplete.cache` uses for the
corpus index, whose primitives this module reuses rather than restating.

Putting the manifest *inside* the generation is what makes the swap complete:
there is no second file to update afterwards, so no window exists in which the
manifest and the index disagree. A process that dies at any point before the
rename leaves the previous generation whole and still serving; the abandoned
directory is removed by the next successful publish.

Because a generation carries its own text, rebuilding after a removal, or after
adding one document to nine existing ones, copies the unchanged documents from
the generation now serving instead of downloading them from Drive again.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from ..cache import (
    CacheMiss,
    current_generation,
    discard_other_generations,
    flush_directory,
    new_generation_name,
    publish_pointer,
)
from ..index import SearchIndex
from ..topk import DEFAULT_BLOCK_SIZE
from .errors import StoreCorruptError
from .settings import DRIVE_SOURCE_PREFIX, GOOGLE_DOC_MIME_TYPE

__all__ = [
    "FORMAT_VERSION",
    "MANIFEST_FILE",
    "SOURCES_DIR",
    "DriveStore",
    "ImportedCorpus",
    "ImportedDocument",
    "PreparedDocument",
    "document_id",
    "safe_filename",
]

#: Bumped when the layout below changes, so an older state is rejected rather
#: than misread.
FORMAT_VERSION = 1

MANIFEST_FILE = "manifest.json"
SOURCES_DIR = "sources"

#: Longest sanitized file name, leaving room for the ``.txt`` suffix and a
#: disambiguating counter inside every filesystem's per-name limit.
_MAX_NAME_LENGTH = 120

#: Anything outside this is replaced. Path separators, control characters and
#: the shell-significant punctuation are all excluded by construction rather
#: than by a blocklist, so a name from Drive cannot address anything.
_ALLOWED_NAME = re.compile(r"[^A-Za-z0-9 ._()\[\]-]")

Logger = Callable[[str], None]


@dataclass(frozen=True)
class ImportedDocument:
    """One document that has been imported, as the manifest records it.

    Attributes:
        id: Internal identifier, derived from the Drive file ID by hashing, so
            it is collision-safe, stable and safe to use in a URL or a path.
        drive_file_id: The Drive file this came from, used to recognise a
            re-import of the same file.
        name: The document's name in Drive, as Drive reported it.
        mime_type: The Drive MIME type it was imported as.
        source_text: The value results report, and the path the text is stored
            at inside a generation. Decided once, at first import, and never
            recomputed, so it is stable as other documents come and go.
        modified_time: Drive's last-modified timestamp, where Drive gave one.
        revision_id: Drive's head revision identifier, where Drive gave one.
        imported_at: When this content was imported, UTC, ISO 8601.
        content_sha256: Fingerprint of the stored text.
        bytes: Size of the stored text.
        sentences: How many lines of it survived normalization and are
            searchable.
        status: ``indexed`` once it is part of a published generation.
    """

    id: str
    drive_file_id: str
    name: str
    mime_type: str
    source_text: str
    modified_time: str | None
    revision_id: str | None
    imported_at: str
    content_sha256: str
    bytes: int
    sentences: int
    status: str = "indexed"

    @property
    def is_google_doc(self) -> bool:
        return self.mime_type == GOOGLE_DOC_MIME_TYPE

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: object) -> "ImportedDocument":
        if not isinstance(data, dict):
            raise StoreCorruptError("a manifest entry is not an object")
        required = (
            "id",
            "drive_file_id",
            "name",
            "mime_type",
            "source_text",
            "imported_at",
            "content_sha256",
        )
        missing = [key for key in required if not isinstance(data.get(key), str)]
        if missing:
            raise StoreCorruptError(
                f"a manifest entry is missing {', '.join(missing)}"
            )
        source_text = data["source_text"]
        if not _is_safe_source_text(source_text):
            raise StoreCorruptError(
                f"a manifest entry names an unsafe source path: {source_text!r}"
            )
        return cls(
            id=data["id"],
            drive_file_id=data["drive_file_id"],
            name=data["name"],
            mime_type=data["mime_type"],
            source_text=source_text,
            modified_time=_optional_text(data.get("modified_time")),
            revision_id=_optional_text(data.get("revision_id")),
            imported_at=data["imported_at"],
            content_sha256=data["content_sha256"],
            bytes=_whole_number(data.get("bytes"), "bytes"),
            sentences=_whole_number(data.get("sentences"), "sentences"),
            status=str(data.get("status", "indexed")),
        )


@dataclass(frozen=True)
class PreparedDocument:
    """A document ready to be written into a new generation.

    Either ``text`` carries freshly downloaded bytes, or ``copy_from`` names the
    file in the generation now serving that already holds them. Exactly one is
    set: a rebuild after a removal re-uses what is already on disk rather than
    asking Drive for it again.
    """

    document: ImportedDocument
    text: bytes | None = None
    copy_from: Path | None = None

    def read(self) -> bytes:
        if self.text is not None:
            return self.text
        if self.copy_from is None:
            raise StoreCorruptError(
                f"no content available for {self.document.source_text}"
            )
        return self.copy_from.read_bytes()


@dataclass(frozen=True)
class ImportedCorpus:
    """A published generation: what is searchable and where it came from.

    ``index`` is ``None`` when the manifest is empty, which is the state after
    the last document is removed. Nothing is built for an empty document set,
    so a search then costs exactly what it costs with the feature switched off.
    """

    documents: tuple[ImportedDocument, ...]
    index: SearchIndex | None
    generation: Path
    fingerprint: str

    def __bool__(self) -> bool:
        return self.index is not None

    @property
    def sentences(self) -> int:
        return 0 if self.index is None else len(self.index)

    @property
    def total_bytes(self) -> int:
        return sum(document.bytes for document in self.documents)

    def source_path(self, document: ImportedDocument) -> Path:
        return self.generation / SOURCES_DIR / document.source_text


class DriveStore:
    """Reads and publishes the imported corpus under one data directory."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        summary_width: int,
        block_size: int = DEFAULT_BLOCK_SIZE,
        use_mmap: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.summary_width = summary_width
        self.block_size = block_size
        self.use_mmap = use_mmap

    # ------------------------------------------------------------- reading ---

    def load(self) -> ImportedCorpus | None:
        """Read the generation the pointer names, or ``None`` if there is none.

        Raises:
            StoreCorruptError: if a generation is published but its manifest
                cannot be read or does not describe what is beside it. That is
                deliberately not treated as "nothing imported": silently
                serving fewer sentences than the user imported would be worse
                than saying the state is unreadable.
        """
        try:
            generation = current_generation(self.data_dir)
        except CacheMiss:
            return None

        manifest = self._read_manifest(generation)
        documents = tuple(
            ImportedDocument.from_json(entry) for entry in manifest["documents"]
        )
        _check_distinct(documents)

        index: SearchIndex | None = None
        if documents:
            try:
                index = SearchIndex.read_from(
                    generation,
                    summary_width=self.summary_width,
                    block_size=self.block_size,
                    use_mmap=self.use_mmap,
                )
                index.check_structure()
            except Exception as exc:
                raise StoreCorruptError(
                    f"the imported index in {generation.name} could not be read: "
                    f"{type(exc).__name__}. Remove the imported data directory "
                    f"to start again: {self.data_dir}"
                ) from exc
            if len(index) != manifest["record_count"]:
                raise StoreCorruptError(
                    f"the imported index in {generation.name} holds "
                    f"{len(index)} sentences, its manifest says "
                    f"{manifest['record_count']}"
                )

        return ImportedCorpus(
            documents=documents,
            index=index,
            generation=generation,
            fingerprint=manifest["fingerprint"],
        )

    def _read_manifest(self, generation: Path) -> dict:
        path = generation / MANIFEST_FILE
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StoreCorruptError(
                f"the imported corpus in {generation.name} has no manifest"
            ) from exc
        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StoreCorruptError(
                f"the imported manifest in {generation.name} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(manifest, dict):
            raise StoreCorruptError("the imported manifest is not an object")
        if manifest.get("format_version") != FORMAT_VERSION:
            raise StoreCorruptError(
                f"the imported corpus was written by format version "
                f"{manifest.get('format_version')!r}, this build expects "
                f"{FORMAT_VERSION}"
            )
        if manifest.get("summary_width") != self.summary_width:
            raise StoreCorruptError(
                f"the imported index answers {manifest.get('summary_width')!r} "
                f"results at a time, the server now asks for {self.summary_width}"
            )
        if not isinstance(manifest.get("documents"), list):
            raise StoreCorruptError("the imported manifest lists no documents")
        if not isinstance(manifest.get("fingerprint"), str):
            raise StoreCorruptError("the imported manifest carries no fingerprint")
        if not isinstance(manifest.get("record_count"), int):
            raise StoreCorruptError("the imported manifest carries no sentence count")
        return manifest

    # ---------------------------------------------------------- publishing ---

    def publish(
        self,
        prepared: Sequence[PreparedDocument],
        *,
        log: Logger | None = None,
    ) -> ImportedCorpus:
        """Write a new generation and make it the one that is served.

        Nothing already published is touched until the very last step, so a
        failure anywhere in here leaves the previous state serving.
        """
        announce = log or (lambda message: None)
        _check_distinct([item.document for item in prepared])

        fingerprint = _fingerprint(item.document for item in prepared)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        generation_dir = self.data_dir / new_generation_name(fingerprint)
        generation_dir.mkdir()

        try:
            sources = generation_dir / SOURCES_DIR
            sources.mkdir()
            for item in prepared:
                destination = sources / item.document.source_text
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(item.read())

            index: SearchIndex | None = None
            documents = tuple(item.document for item in prepared)
            if documents:
                announce(f"indexing {len(documents)} document(s)")
                index = SearchIndex.build(
                    sources,
                    summary_width=self.summary_width,
                    block_size=self.block_size,
                )
                index.check_structure()
                index.write_to(generation_dir)
                documents = _count_sentences(documents, index)

            (generation_dir / MANIFEST_FILE).write_text(
                json.dumps(
                    _manifest(documents, index, fingerprint, self.summary_width),
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            flush_directory(generation_dir)
        except Exception:
            # The pointer has not moved, so this directory is unreachable. Take
            # it away rather than leaving it to be mistaken for a state.
            shutil.rmtree(generation_dir, ignore_errors=True)
            raise

        publish_pointer(self.data_dir, generation_dir.name)
        discard_other_generations(self.data_dir, keep=generation_dir.name)
        announce(f"published {generation_dir.name}")

        published = self.load()
        if published is None:  # pragma: no cover - the pointer was just written
            raise StoreCorruptError("the generation just published cannot be read")
        return published

    # ------------------------------------------------------------- naming ----

    def choose_source_text(self, name: str, taken: Iterable[str]) -> str:
        """Pick the ``source_text`` a newly imported document keeps for good.

        Decided once and recorded in the manifest, never recomputed, so adding
        or removing other documents cannot change what an existing result says
        it came from.
        """
        reserved = set(taken)
        stem = safe_filename(name)
        candidate = f"{DRIVE_SOURCE_PREFIX}/{stem}.txt"
        counter = 2
        while candidate in reserved:
            candidate = f"{DRIVE_SOURCE_PREFIX}/{stem} ({counter}).txt"
            counter += 1
        return candidate


def document_id(drive_file_id: str) -> str:
    """A stable, collision-safe internal identifier for a Drive file.

    Hashed rather than used directly, so the identifier is safe in a path or a
    URL whatever Drive's own identifier contains, and so a raw Drive file ID
    does not end up in a filename.
    """
    return hashlib.sha256(drive_file_id.encode("utf-8")).hexdigest()[:16]


def safe_filename(name: str) -> str:
    """Turn a name from Drive into a file name that can only ever be a name.

    Only the final component is considered, so ``../../etc/passwd`` becomes
    ``passwd``; then everything outside a small allowed set is replaced, so no
    separator, control character or shell metacharacter can survive. A name
    that empties out becomes ``document``, because a result has to say
    something.
    """
    # Take the last component under both separators, so a Windows-style name
    # cannot smuggle a directory through a POSIX split.
    text = unicodedata.normalize("NFKC", name or "").replace("\\", "/")
    text = PurePosixPath(text).name

    if text.lower().endswith(".txt"):
        text = text[: -len(".txt")]

    text = _ALLOWED_NAME.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    text = text[:_MAX_NAME_LENGTH].strip(" ._")
    return text or "document"


def _is_safe_source_text(value: str) -> bool:
    """Whether a manifest's stored path is one this store could have written."""
    if not value.startswith(f"{DRIVE_SOURCE_PREFIX}/") or not value.endswith(".txt"):
        return False
    remainder = value[len(DRIVE_SOURCE_PREFIX) + 1 :]
    if "/" in remainder or "\\" in remainder:
        return False
    return remainder not in ("", ".txt") and ".." not in remainder


def _check_distinct(documents: Sequence[ImportedDocument]) -> None:
    for field_name in ("id", "source_text", "drive_file_id"):
        values = [getattr(document, field_name) for document in documents]
        if len(set(values)) != len(values):
            raise StoreCorruptError(
                f"two imported documents share the same {field_name}"
            )


def _fingerprint(documents: Iterable[ImportedDocument]) -> str:
    """Hash what the imported corpus consists of, for naming and validation."""
    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item.source_text):
        for part in (document.source_text, document.content_sha256):
            encoded = part.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "little"))
            digest.update(encoded)
    return digest.hexdigest()


def _count_sentences(
    documents: Sequence[ImportedDocument], index: SearchIndex
) -> tuple[ImportedDocument, ...]:
    """Fill in how many searchable sentences each document contributed."""
    counts: dict[str, int] = {}
    for record in range(len(index)):
        source = index.records.source_text(record)
        counts[source] = counts.get(source, 0) + 1
    return tuple(
        replace(document, sentences=counts.get(document.source_text, 0))
        for document in documents
    )


def _manifest(
    documents: Sequence[ImportedDocument],
    index: SearchIndex | None,
    fingerprint: str,
    summary_width: int,
) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "fingerprint": fingerprint,
        "summary_width": summary_width,
        "record_count": 0 if index is None else len(index),
        "documents": [document.to_json() for document in documents],
        "index": None if index is None else index.describe(),
    }


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _whole_number(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StoreCorruptError(f"a manifest entry has an invalid {name}: {value!r}")
    return value
