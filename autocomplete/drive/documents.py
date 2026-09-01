"""Turning a file the user picked in Drive into corpus text.

Every check here happens on what *Drive* said the file is, never on what the
browser claimed, and the cheap refusals happen before anything is downloaded:
an unsupported type or an oversized file costs one metadata request and no
transfer at all.

Line boundaries
---------------

A corpus sentence is one line of one file, so line boundaries are the thing this
module must not lose. Both endings are normalized to ``\\n`` before the text is
stored, which matters for more than tidiness: a file written on an old Mac uses
a lone carriage return, and leaving those in place would collapse the whole file
into a single enormous "sentence" with every result reporting line 1.

For a native Google Doc there are no line boundaries to preserve, only paragraph
ones. Drive's plain-text export ends each paragraph with a newline, so **one
paragraph becomes one corpus sentence**. That is the closest honest mapping, and
it has a consequence worth stating: a long paragraph is one long sentence rather
than the several the reader sees on screen, and a sentence that a writer split
across two paragraphs is two records that each match only their own half.
Formatting, tables, images, comments and footnotes are whatever the export makes
of them, and are indexed as they arrive.

Encoding
--------

The base corpus decodes with ``errors="replace"``: those files are given, and a
stray byte should not stop a build. An imported document is different, because
there is a person watching who chose the file and can choose a different one. So
decoding is strict, and a file that is not UTF-8 is refused with a message that
says so rather than being indexed as replacement characters.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from .client import DriveClient, DriveFile
from .errors import (
    DocumentTooLargeError,
    InvalidEncodingError,
    UnsupportedDocumentError,
)
from .settings import (
    GOOGLE_DOC_MIME_TYPE,
    PLAIN_TEXT_MIME_TYPE,
    DriveSettings,
)
from .store import ImportedDocument, PreparedDocument, document_id

__all__ = [
    "TEXT_SUFFIX",
    "decode_text",
    "fetch_document",
    "normalize_line_endings",
    "check_supported",
]

TEXT_SUFFIX = ".txt"

_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class _Fetched:
    text: bytes
    sha256: str


def check_supported(metadata: DriveFile, settings: DriveSettings) -> None:
    """Refuse a file this feature does not import, before downloading it.

    Raises:
        UnsupportedDocumentError: for any type outside the configured list, for
            a plain file whose name does not end in ``.txt``, and for a file the
            account is not allowed to download.
        DocumentTooLargeError: when Drive already reports it as over the limit.
    """
    if metadata.mime_type not in settings.supported_mime_types:
        raise UnsupportedDocumentError(
            f"{_describe(metadata)} is a {metadata.mime_type} file. This import "
            f"handles plain text files and Google Docs only."
        )

    # A stored file is read byte for byte, so its extension has to agree with
    # its type: "text/plain" on a file named .csv or .md would be indexed as
    # sentences it was never meant to be split into.
    if metadata.mime_type == PLAIN_TEXT_MIME_TYPE and not metadata.name.lower().endswith(
        TEXT_SUFFIX
    ):
        raise UnsupportedDocumentError(
            f"{_describe(metadata)} is plain text but is not named .txt. Rename "
            f"it in Drive, or select a .txt file or a Google Doc."
        )

    if not metadata.can_download:
        raise UnsupportedDocumentError(
            f"{_describe(metadata)} cannot be downloaded with this Google account."
        )

    if metadata.size is not None and metadata.size > settings.max_file_bytes:
        raise DocumentTooLargeError(
            f"{_describe(metadata)} is {metadata.size:,} bytes, over the "
            f"{settings.max_file_bytes:,} byte limit."
        )


def fetch_document(
    client: DriveClient,
    metadata: DriveFile,
    settings: DriveSettings,
    *,
    source_text: str,
    now: datetime | None = None,
) -> PreparedDocument:
    """Download or export one file and prepare it for indexing.

    ``source_text`` is decided by the caller and kept for the life of the
    document, so a result always reports the same origin.
    """
    check_supported(metadata, settings)

    if metadata.mime_type == GOOGLE_DOC_MIME_TYPE:
        raw = client.export_text(metadata.file_id, max_bytes=settings.max_file_bytes)
    else:
        raw = client.download(metadata.file_id, max_bytes=settings.max_file_bytes)

    if len(raw) > settings.max_file_bytes:
        raise DocumentTooLargeError(
            f"{_describe(metadata)} is over the {settings.max_file_bytes:,} byte limit."
        )

    text = normalize_line_endings(decode_text(raw, metadata))
    stamp = (now or datetime.now(timezone.utc)).isoformat()

    return PreparedDocument(
        document=ImportedDocument(
            id=document_id(metadata.file_id),
            drive_file_id=metadata.file_id,
            name=metadata.name,
            mime_type=metadata.mime_type,
            source_text=source_text,
            modified_time=metadata.modified_time,
            revision_id=metadata.revision_id,
            imported_at=stamp,
            content_sha256=hashlib.sha256(text).hexdigest(),
            bytes=len(text),
            sentences=0,
        ),
        text=text,
    )


def decode_text(raw: bytes, metadata: DriveFile) -> bytes:
    """Check the bytes are UTF-8 text, and hand back the text without its mark.

    Returns bytes rather than a string because that is what is written to disk
    and what the corpus reader takes; decoding here is a validation step, not a
    conversion.
    """
    if raw.startswith(_UTF8_BOM):
        raw = raw[len(_UTF8_BOM) :]

    # A NUL byte is the clearest signal that a file claiming to be text is not.
    # UTF-8 decoding would accept it happily, and the corpus would then hold a
    # record no query could ever match.
    if b"\x00" in raw:
        raise InvalidEncodingError(
            f"{_describe(metadata)} contains binary data, not UTF-8 text. Export "
            f"it as plain text and import that."
        )

    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidEncodingError(
            f"{_describe(metadata)} is not valid UTF-8 text (byte {exc.start} is "
            f"not). Re-save it as UTF-8 in Drive and import it again."
        ) from exc
    return raw


def normalize_line_endings(raw: bytes) -> bytes:
    """Make every line ending a single ``\\n``, and end the text with one.

    Done before storing rather than at read time so that what is stored is
    exactly what was indexed, and so a document's line numbers mean the same
    thing however it was written.
    """
    text = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if text and not text.endswith(b"\n"):
        text += b"\n"
    return text


def _describe(metadata: DriveFile) -> str:
    """Name a file for an error message, without letting it become markup.

    The name comes from Drive and is shown to the user, so it is quoted and
    stripped of anything that would run lines together in a log or a message.
    """
    name = "".join(
        character if character.isprintable() else " " for character in metadata.name
    ).strip()
    return f'"{name[:80]}"' if name else "The selected file"
